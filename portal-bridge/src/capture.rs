// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

use std::collections::{HashMap, HashSet};
use std::os::unix::net::UnixStream;

use anyhow::{Context as _, Result, anyhow, bail};
use ashpd::Error as PortalError;
use ashpd::desktop::PersistMode;
use ashpd::desktop::input_capture::{
    ActivatedBarrier, Barrier, BarrierID, Capabilities, ConnectToEISOptions, CreateSession2Options,
    CreateSessionOptions, DisableOptions, EnableOptions, InputCapture, ReleaseOptions,
    SetPointerBarriersOptions, StartOptions,
};
use futures_util::StreamExt;
use reis::ei::{self, button, keyboard};
use reis::event::{DeviceCapability, EiEvent};
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::protocol::{Edge, event};

type Output = mpsc::Sender<Value>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Zone {
    pub width: u32,
    pub height: u32,
    pub x: i32,
    pub y: i32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CaptureTargetSpec {
    pub edge: Edge,
    pub zone: Option<Zone>,
    pub target: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct BarrierSpec {
    id: u32,
    position: [i32; 4],
    edge: Edge,
    target: Option<String>,
}

#[derive(Debug)]
struct ActiveCapture {
    activation_id: Option<u32>,
    cursor_position: Option<(f32, f32)>,
    edge: Option<Edge>,
}

enum CaptureControl {
    Release {
        cursor_position: Option<[f64; 2]>,
        reply: oneshot::Sender<Result<(), String>>,
    },
    Enable {
        reply: oneshot::Sender<Result<(), String>>,
    },
    Disable {
        reply: oneshot::Sender<Result<(), String>>,
    },
    Stop {
        reply: oneshot::Sender<Result<(), String>>,
    },
}

pub struct CaptureHandle {
    control: mpsc::Sender<CaptureControl>,
    join: JoinHandle<()>,
}

impl CaptureHandle {
    /// Report whether the portal session behind this handle is still alive.
    pub fn is_running(&self) -> bool {
        !self.join.is_finished()
    }

    pub async fn release(&self, cursor_position: Option<[f64; 2]>) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(CaptureControl::Release {
                cursor_position,
                reply,
            })
            .await
            .map_err(|_| anyhow!("input capture task is not running"))?;
        response
            .await
            .map_err(|_| anyhow!("input capture task stopped without replying"))?
            .map_err(anyhow::Error::msg)
    }

    pub async fn enable(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(CaptureControl::Enable { reply })
            .await
            .map_err(|_| anyhow!("input capture task is not running"))?;
        response
            .await
            .map_err(|_| anyhow!("input capture task stopped without replying"))?
            .map_err(anyhow::Error::msg)
    }

    pub async fn disable(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(CaptureControl::Disable { reply })
            .await
            .map_err(|_| anyhow!("input capture task is not running"))?;
        response
            .await
            .map_err(|_| anyhow!("input capture task stopped without replying"))?
            .map_err(anyhow::Error::msg)
    }

    pub async fn stop(self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(CaptureControl::Stop { reply })
            .await
            .map_err(|_| anyhow!("input capture task is not running"))?;
        let result = response
            .await
            .map_err(|_| anyhow!("input capture task stopped without replying"))?
            .map_err(anyhow::Error::msg);
        let _ = self.join.await;
        result
    }
}

pub async fn start_capture(
    targets: Vec<CaptureTargetSpec>,
    restore_token: Option<String>,
    output: Output,
) -> Result<(CaptureHandle, Value)> {
    if targets.is_empty() {
        bail!("at least one input capture target is required");
    }
    let (control, control_rx) = mpsc::channel(8);
    let (ready, ready_rx) = oneshot::channel();
    let task_output = output.clone();
    // reis keeps non-Send callback state in its event converter, so EI tasks must
    // stay on the bridge's LocalSet.
    let join = tokio::task::spawn_local(async move {
        let mut ready = Some(ready);
        if let Err(error) = run_capture(
            targets,
            restore_token,
            task_output.clone(),
            control_rx,
            &mut ready,
        )
        .await
        {
            let message = format!("{error:#}");
            if let Some(ready) = ready.take() {
                let _ = ready.send(Err(message));
            } else {
                let _ = task_output
                    .send(event("capture_error", json!({ "error": message })))
                    .await;
            }
        }
    });

    let info = ready_rx
        .await
        .map_err(|_| anyhow!("input capture task stopped during initialization"))?
        .map_err(anyhow::Error::msg)?;
    Ok((CaptureHandle { control, join }, info))
}

async fn run_capture(
    targets: Vec<CaptureTargetSpec>,
    restore_token: Option<String>,
    output: Output,
    mut control: mpsc::Receiver<CaptureControl>,
    ready: &mut Option<oneshot::Sender<Result<Value, String>>>,
) -> Result<()> {
    let portal = InputCapture::new()
        .await
        .context("InputCapture portal is unavailable")?;
    let requested = Capabilities::Keyboard | Capabilities::Pointer;

    let mut returned_restore_token = None;
    let (session, capabilities) = match portal
        .create_session2(CreateSession2Options::default())
        .await
    {
        Ok(session) => {
            let response = portal
                .start(
                    &session,
                    None,
                    StartOptions::default()
                        .set_capabilities(requested)
                        .set_restore_token(restore_token)
                        .set_persist_mode(PersistMode::ExplicitlyRevoked),
                )
                .await?
                .response()?;
            returned_restore_token = response.restore_token().map(ToOwned::to_owned);
            (session, response.capabilities())
        }
        Err(PortalError::RequiresVersion(_, _)) => {
            let (session, capabilities) = portal
                .create_session(
                    None,
                    CreateSessionOptions::default().set_capabilities(requested),
                )
                .await?;
            (session, capabilities)
        }
        Err(error) => return Err(error.into()),
    };

    let fd = portal
        .connect_to_eis(&session, ConnectToEISOptions::default())
        .await
        .context("failed to connect InputCapture to EIS")?;
    let stream = UnixStream::from(fd);
    stream.set_nonblocking(true)?;
    let context = ei::Context::new(stream)?;
    let (_connection, mut events) = context
        .handshake_tokio(
            "mwb-portal-bridge-capture",
            ei::handshake::ContextType::Receiver,
        )
        .await
        .context("InputCapture EI handshake failed")?;

    let zones_response = portal
        .zones(&session, Default::default())
        .await?
        .response()?;
    let zones = zones_response
        .regions()
        .iter()
        .map(|region| Zone {
            width: region.width(),
            height: region.height(),
            x: region.x_offset(),
            y: region.y_offset(),
        })
        .collect::<Vec<_>>();
    let barrier_specs = barriers_for_targets(&targets, &zones)?;
    let barriers = barrier_specs
        .iter()
        .map(|spec| {
            Barrier::new(
                BarrierID::new(spec.id).expect("barrier identifiers start at one"),
                (
                    spec.position[0],
                    spec.position[1],
                    spec.position[2],
                    spec.position[3],
                ),
            )
        })
        .collect::<Vec<_>>();
    let barrier_response = portal
        .set_pointer_barriers(
            &session,
            &barriers,
            zones_response.zone_set(),
            SetPointerBarriersOptions::default(),
        )
        .await?
        .response()?;
    let failed = barrier_response
        .failed_barriers()
        .iter()
        .map(|id| id.get())
        .collect::<Vec<_>>();
    let accepted = accepted_barriers(&barrier_specs, &failed)?;
    let routes = barrier_routes(&accepted);

    let mut activated = portal.receive_activated().await?;
    let mut deactivated = portal.receive_deactivated().await?;
    let mut disabled = portal.receive_disabled().await?;
    let mut zones_changed = portal.receive_zones_changed().await?;
    portal.enable(&session, EnableOptions::default()).await?;

    let info = json!({
        "portal_version": portal.version(),
        "edge": targets.first().map(|target| target.edge),
        "capabilities": format!("{capabilities:?}"),
        "restore_token": returned_restore_token,
        "targets": targets.iter().map(|target| json!({
            "edge": target.edge,
            "zone": target.zone.map(|zone| json!([
                zone.x, zone.y, zone.width, zone.height
            ])),
            "target": target.target,
        })).collect::<Vec<_>>(),
        "zones": zones.iter().map(|zone| json!({
            "width": zone.width,
            "height": zone.height,
            "x": zone.x,
            "y": zone.y,
        })).collect::<Vec<_>>(),
        "barriers": barrier_metadata(barrier_specs.iter()),
        "accepted_barriers": barrier_metadata(accepted.iter()),
        "failed_barriers": failed,
        "failed_barrier_metadata": barrier_metadata(
            barrier_specs.iter().filter(|spec| failed.contains(&spec.id))
        ),
    });
    ready
        .take()
        .expect("ready result is sent once")
        .send(Ok(info))
        .map_err(|_| anyhow!("command reader stopped during input capture initialization"))?;

    let mut active: Option<ActiveCapture> = None;
    loop {
        tokio::select! {
            command = control.recv() => {
                let Some(command) = command else {
                    session.close().await?;
                    break;
                };
                match command {
                    CaptureControl::Release { cursor_position, reply } => {
                        let result = release_capture(&portal, &session, &mut active, cursor_position)
                            .await
                            .map_err(|error| format!("{error:#}"));
                        let _ = reply.send(result);
                    }
                    CaptureControl::Enable { reply } => {
                        let result = portal
                            .enable(&session, EnableOptions::default())
                            .await
                            .map_err(|error| error.to_string());
                        let _ = reply.send(result);
                    }
                    CaptureControl::Disable { reply } => {
                        let release_result = if active.is_some() {
                            release_capture(&portal, &session, &mut active, None)
                                .await
                                .map_err(|error| format!("{error:#}"))
                        } else {
                            Ok(())
                        };
                        let result = match release_result {
                            Ok(()) => portal
                                .disable(&session, DisableOptions::default())
                                .await
                                .map_err(|error| error.to_string()),
                            Err(error) => Err(error),
                        };
                        let _ = reply.send(result);
                    }
                    CaptureControl::Stop { reply } => {
                        let result = session.close().await.map_err(|error| error.to_string());
                        let _ = reply.send(result);
                        break;
                    }
                }
            }
            signal = activated.next() => {
                let Some(signal) = signal else {
                    bail!("InputCapture Activated signal stream ended");
                };
                let barrier_id = match signal.barrier_id() {
                    Some(ActivatedBarrier::Barrier(id)) => Some(id.get()),
                    Some(ActivatedBarrier::UnknownBarrier) | None => None,
                };
                let route = barrier_id.and_then(|id| routes.get(&id));
                active = Some(ActiveCapture {
                    activation_id: signal.activation_id(),
                    cursor_position: signal.cursor_position(),
                    edge: route.map(|(edge, _)| *edge),
                });
                output.send(event("capture_activated", json!({
                    "activation_id": signal.activation_id(),
                    "barrier_id": barrier_id,
                    "edge": route.map(|(edge, _)| *edge),
                    "target": route.and_then(|(_, target)| target.as_deref()),
                    "cursor_position": signal.cursor_position(),
                }))).await.map_err(|_| anyhow!("stdout writer stopped"))?;
            }
            signal = deactivated.next() => {
                let Some(signal) = signal else {
                    bail!("InputCapture Deactivated signal stream ended");
                };
                active = None;
                output.send(event("capture_deactivated", json!({
                    "activation_id": signal.activation_id(),
                }))).await.map_err(|_| anyhow!("stdout writer stopped"))?;
            }
            signal = disabled.next() => {
                if signal.is_none() {
                    bail!("InputCapture Disabled signal stream ended");
                }
                active = None;
                output.send(event("capture_disabled", json!({}))).await
                    .map_err(|_| anyhow!("stdout writer stopped"))?;
            }
            signal = zones_changed.next() => {
                let Some(signal) = signal else {
                    bail!("InputCapture ZonesChanged signal stream ended");
                };
                output.send(event("capture_zones_changed", json!({
                    "zone_set": signal.zone_set(),
                    "requires_reinitialize": true,
                }))).await.map_err(|_| anyhow!("stdout writer stopped"))?;
            }
            ei_event = events.next() => {
                let Some(ei_event) = ei_event else {
                    bail!("InputCapture EIS connection closed");
                };
                handle_capture_event(ei_event?, &context, &output).await?;
            }
        }
    }

    Ok(())
}

async fn release_capture(
    portal: &InputCapture,
    session: &ashpd::desktop::Session<InputCapture>,
    active: &mut Option<ActiveCapture>,
    cursor_position: Option<[f64; 2]>,
) -> Result<()> {
    let activation = active
        .take()
        .ok_or_else(|| anyhow!("input capture is not active"))?;
    let cursor_position = release_position(&activation, cursor_position);
    let result = portal
        .release(
            session,
            ReleaseOptions::default()
                .set_activation_id(activation.activation_id)
                .set_cursor_position(cursor_position),
        )
        .await;
    if let Err(error) = result {
        // Retain the activation state so the caller can retry a safety-critical
        // release after a transient portal failure.
        *active = Some(activation);
        return Err(error.into());
    }
    Ok(())
}

async fn handle_capture_event(
    event_value: EiEvent,
    context: &ei::Context,
    output: &Output,
) -> Result<()> {
    let value = match event_value {
        EiEvent::SeatAdded(event_value) => {
            event_value.seat.bind_capabilities(&[
                DeviceCapability::Pointer,
                DeviceCapability::PointerAbsolute,
                DeviceCapability::Keyboard,
                DeviceCapability::Scroll,
                DeviceCapability::Button,
            ]);
            context.flush()?;
            Some(event(
                "capture_seat_added",
                json!({ "name": event_value.seat.name() }),
            ))
        }
        EiEvent::DeviceAdded(event_value) => Some(event(
            "capture_device_added",
            json!({
                "name": event_value.device.name(),
                "keyboard": event_value.device.has_capability(DeviceCapability::Keyboard),
                "pointer": event_value.device.has_capability(DeviceCapability::Pointer),
                "pointer_absolute": event_value.device.has_capability(DeviceCapability::PointerAbsolute),
                "button": event_value.device.has_capability(DeviceCapability::Button),
                "scroll": event_value.device.has_capability(DeviceCapability::Scroll),
            }),
        )),
        EiEvent::KeyboardKey(event_value) => Some(event(
            "key",
            json!({
                "keycode": event_value.key,
                "state": match event_value.state {
                    keyboard::KeyState::Press => "pressed",
                    keyboard::KeyState::Released => "released",
                },
                "time_us": event_value.time,
            }),
        )),
        EiEvent::PointerMotion(event_value) => Some(event(
            "pointer_motion",
            json!({ "dx": event_value.dx, "dy": event_value.dy, "time_us": event_value.time }),
        )),
        EiEvent::PointerMotionAbsolute(event_value) => Some(event(
            "pointer_absolute",
            json!({
                "x": event_value.dx_absolute,
                "y": event_value.dy_absolute,
                "time_us": event_value.time,
            }),
        )),
        EiEvent::Button(event_value) => Some(event(
            "button",
            json!({
                "button": event_value.button,
                "state": match event_value.state {
                    button::ButtonState::Press => "pressed",
                    button::ButtonState::Released => "released",
                },
                "time_us": event_value.time,
            }),
        )),
        EiEvent::ScrollDelta(event_value) => Some(event(
            "scroll",
            json!({
                "dx": event_value.dx,
                "dy": event_value.dy,
                "discrete": false,
                "time_us": event_value.time,
            }),
        )),
        EiEvent::ScrollDiscrete(event_value) => Some(event(
            "scroll",
            json!({
                "dx": event_value.discrete_dx,
                "dy": event_value.discrete_dy,
                "discrete": true,
                "time_us": event_value.time,
            }),
        )),
        EiEvent::ScrollStop(event_value) => Some(event(
            "scroll_stop",
            json!({ "x": event_value.x, "y": event_value.y, "cancelled": false, "time_us": event_value.time }),
        )),
        EiEvent::ScrollCancel(event_value) => Some(event(
            "scroll_stop",
            json!({ "x": event_value.x, "y": event_value.y, "cancelled": true, "time_us": event_value.time }),
        )),
        EiEvent::Disconnected(event_value) => {
            bail!("EIS disconnected: {}", event_value.explanation)
        }
        _ => None,
    };

    if let Some(value) = value {
        output
            .send(value)
            .await
            .map_err(|_| anyhow!("stdout writer stopped"))?;
    }
    Ok(())
}

/// Calculates the pointer barriers for `edge`.
///
/// `target` names the monitor the settings matrix placed the Windows host
/// against; when it matches a reported zone the barrier covers only that
/// monitor, so the pointer crosses over at exactly the screen the user drew.
pub fn barrier_positions(
    edge: Edge,
    zones: &[Zone],
    target: Option<Zone>,
) -> Result<Vec<(u32, [i32; 4])>> {
    if zones.is_empty() {
        bail!("the compositor returned no input capture zones");
    }

    let dimensions = zones
        .iter()
        .map(|zone| {
            let width = i32::try_from(zone.width).context("zone width exceeds i32")?;
            let height = i32::try_from(zone.height).context("zone height exceeds i32")?;
            Ok((zone, width, height))
        })
        .collect::<Result<Vec<_>>>()?;
    let exterior = match edge {
        Edge::Left => dimensions.iter().map(|(zone, _, _)| zone.x).min().unwrap(),
        Edge::Right => dimensions
            .iter()
            .map(|(zone, width, _)| zone.x + width)
            .max()
            .unwrap(),
        Edge::Top => dimensions.iter().map(|(zone, _, _)| zone.y).min().unwrap(),
        Edge::Bottom => dimensions
            .iter()
            .map(|(zone, _, height)| zone.y + height)
            .max()
            .unwrap(),
    };

    // An unknown target means the monitor layout changed since the settings
    // were saved; fall back to every exterior monitor rather than failing.
    let target_index =
        target.and_then(|wanted| dimensions.iter().position(|(zone, _, _)| **zone == wanted));
    let barriers = dimensions
        .into_iter()
        .enumerate()
        .filter(|(index, _)| target_index.is_none_or(|wanted| wanted == *index))
        .filter_map(|(index, (zone, width, height))| {
            let position = match edge {
                Edge::Left if zone.x == exterior => {
                    Some([zone.x, zone.y, zone.x, zone.y + height - 1])
                }
                Edge::Right if zone.x + width == exterior => {
                    Some([zone.x + width, zone.y, zone.x + width, zone.y + height - 1])
                }
                Edge::Top if zone.y == exterior => {
                    Some([zone.x, zone.y, zone.x + width - 1, zone.y])
                }
                Edge::Bottom if zone.y + height == exterior => {
                    Some([zone.x, zone.y + height, zone.x + width - 1, zone.y + height])
                }
                _ => None,
            };
            position.map(|position| ((index + 1) as u32, position))
        })
        .collect::<Vec<_>>();

    if barriers.is_empty() {
        if target_index.is_some() {
            bail!(
                "the {edge:?} side of the selected monitor faces another monitor, \
                 so the pointer cannot cross there"
            );
        }
        bail!("could not calculate an exterior {edge:?} barrier");
    }
    Ok(barriers)
}

fn barriers_for_targets(targets: &[CaptureTargetSpec], zones: &[Zone]) -> Result<Vec<BarrierSpec>> {
    if targets.is_empty() {
        bail!("at least one input capture target is required");
    }

    let mut barriers = Vec::new();
    let mut next_id = 1_u32;
    for target in targets {
        for (_, position) in barrier_positions(target.edge, zones, target.zone)? {
            barriers.push(BarrierSpec {
                id: next_id,
                position,
                edge: target.edge,
                target: target.target.clone(),
            });
            next_id = next_id
                .checked_add(1)
                .ok_or_else(|| anyhow!("too many input capture barriers"))?;
        }
    }
    Ok(barriers)
}

fn accepted_barriers(barriers: &[BarrierSpec], failed: &[u32]) -> Result<Vec<BarrierSpec>> {
    let failed = failed.iter().copied().collect::<HashSet<_>>();
    let accepted = barriers
        .iter()
        .filter(|barrier| !failed.contains(&barrier.id))
        .cloned()
        .collect::<Vec<_>>();
    if accepted.is_empty() {
        bail!("the compositor rejected every requested pointer barrier");
    }
    Ok(accepted)
}

fn barrier_routes(barriers: &[BarrierSpec]) -> HashMap<u32, (Edge, Option<String>)> {
    barriers
        .iter()
        .map(|barrier| (barrier.id, (barrier.edge, barrier.target.clone())))
        .collect()
}

fn barrier_metadata<'a>(barriers: impl IntoIterator<Item = &'a BarrierSpec>) -> Vec<Value> {
    barriers
        .into_iter()
        .map(|barrier| {
            json!({
                "id": barrier.id,
                "position": barrier.position,
                "edge": barrier.edge,
                "target": barrier.target,
            })
        })
        .collect()
}

fn release_position(active: &ActiveCapture, explicit: Option<[f64; 2]>) -> Option<(f64, f64)> {
    explicit
        .map(|position| (position[0], position[1]))
        .or_else(|| {
            active
                .cursor_position
                .zip(active.edge)
                .map(|(position, edge)| nudge_inside(edge, position))
        })
}

fn nudge_inside(edge: Edge, position: (f32, f32)) -> (f64, f64) {
    let (x, y) = (f64::from(position.0), f64::from(position.1));
    match edge {
        Edge::Left => (x + 1.0, y),
        Edge::Right => (x - 1.0, y),
        Edge::Top => (x, y + 1.0),
        Edge::Bottom => (x, y - 1.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SIDE_BY_SIDE: [Zone; 2] = [
        Zone {
            width: 1920,
            height: 1080,
            x: 0,
            y: 0,
        },
        Zone {
            width: 2560,
            height: 1440,
            x: 1920,
            y: 0,
        },
    ];

    #[test]
    fn right_barrier_uses_only_union_exterior() {
        assert_eq!(
            barrier_positions(Edge::Right, &SIDE_BY_SIDE, None).unwrap(),
            vec![(2, [4480, 0, 4480, 1439])]
        );
    }

    #[test]
    fn top_barriers_cover_both_zones() {
        assert_eq!(
            barrier_positions(Edge::Top, &SIDE_BY_SIDE, None).unwrap(),
            vec![(1, [0, 0, 1919, 0]), (2, [1920, 0, 4479, 0])]
        );
    }

    #[test]
    fn target_zone_limits_the_barrier_to_one_monitor() {
        assert_eq!(
            barrier_positions(Edge::Top, &SIDE_BY_SIDE, Some(SIDE_BY_SIDE[1])).unwrap(),
            vec![(2, [1920, 0, 4479, 0])]
        );
    }

    #[test]
    fn target_zone_facing_another_monitor_is_rejected() {
        let error = barrier_positions(Edge::Right, &SIDE_BY_SIDE, Some(SIDE_BY_SIDE[0]))
            .unwrap_err()
            .to_string();
        assert!(error.contains("faces another monitor"), "{error}");
    }

    #[test]
    fn unknown_target_zone_falls_back_to_the_exterior() {
        let detached = Zone {
            width: 800,
            height: 600,
            x: -800,
            y: 0,
        };
        assert_eq!(
            barrier_positions(Edge::Right, &SIDE_BY_SIDE, Some(detached)).unwrap(),
            vec![(2, [4480, 0, 4480, 1439])]
        );
    }

    #[test]
    fn empty_zone_set_is_rejected() {
        assert!(barrier_positions(Edge::Left, &[], None).is_err());
    }

    #[test]
    fn multi_target_barriers_have_unique_ids_and_routes() {
        let targets = [
            CaptureTargetSpec {
                edge: Edge::Top,
                zone: None,
                target: Some("alpha".into()),
            },
            CaptureTargetSpec {
                edge: Edge::Right,
                zone: Some(SIDE_BY_SIDE[1]),
                target: Some("beta".into()),
            },
        ];

        let barriers = barriers_for_targets(&targets, &SIDE_BY_SIDE).unwrap();

        assert_eq!(
            barriers,
            vec![
                BarrierSpec {
                    id: 1,
                    position: [0, 0, 1919, 0],
                    edge: Edge::Top,
                    target: Some("alpha".into()),
                },
                BarrierSpec {
                    id: 2,
                    position: [1920, 0, 4479, 0],
                    edge: Edge::Top,
                    target: Some("alpha".into()),
                },
                BarrierSpec {
                    id: 3,
                    position: [4480, 0, 4480, 1439],
                    edge: Edge::Right,
                    target: Some("beta".into()),
                },
            ]
        );
        let routes = barrier_routes(&barriers);
        assert_eq!(routes.get(&1), Some(&(Edge::Top, Some("alpha".into()))));
        assert_eq!(routes.get(&3), Some(&(Edge::Right, Some("beta".into()))));
    }

    #[test]
    fn empty_multi_target_set_is_rejected() {
        assert!(barriers_for_targets(&[], &SIDE_BY_SIDE).is_err());
    }

    #[test]
    fn failed_barriers_keep_metadata_and_reject_all_failed_sets() {
        let targets = [CaptureTargetSpec {
            edge: Edge::Top,
            zone: None,
            target: Some("alpha".into()),
        }];
        let barriers = barriers_for_targets(&targets, &SIDE_BY_SIDE).unwrap();

        let accepted = accepted_barriers(&barriers, &[1]).unwrap();
        assert_eq!(accepted, vec![barriers[1].clone()]);
        assert_eq!(
            barrier_metadata(accepted.iter()),
            vec![json!({
                "id": 2,
                "position": [1920, 0, 4479, 0],
                "edge": "top",
                "target": "alpha",
            })]
        );
        assert!(accepted_barriers(&barriers, &[1, 2]).is_err());
    }

    #[test]
    fn release_position_moves_back_inside_local_zone() {
        assert_eq!(nudge_inside(Edge::Left, (-2.0, 40.0)), (-1.0, 40.0));
        assert_eq!(nudge_inside(Edge::Bottom, (20.0, 1084.0)), (20.0, 1083.0));
    }

    #[test]
    fn release_uses_the_edge_of_the_activated_barrier() {
        let active = ActiveCapture {
            activation_id: Some(7),
            cursor_position: Some((4480.0, 400.0)),
            edge: Some(Edge::Right),
        };

        assert_eq!(release_position(&active, None), Some((4479.0, 400.0)));
        assert_eq!(
            release_position(&active, Some([120.0, 240.0])),
            Some((120.0, 240.0))
        );
    }
}
