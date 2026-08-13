// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

use std::os::unix::net::UnixStream;
use std::time::Duration;

use anyhow::{Context as _, Result, anyhow, bail};
use ashpd::desktop::PersistMode;
use ashpd::desktop::remote_desktop::{
    ConnectToEISOptions, DeviceType, RemoteDesktop, SelectDevicesOptions, StartOptions,
};
use futures_util::StreamExt;
use reis::ei::{self, button, keyboard};
use reis::event::{Connection, Device, DeviceCapability, EiEvent};
use rustix::time::{ClockId, clock_gettime};
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::protocol::{ButtonState, KeyState, event};

type Output = mpsc::Sender<Value>;

#[derive(Clone, Copy, Debug)]
pub enum InjectAction {
    Key { keycode: u32, state: KeyState },
    PointerMotion { dx: f32, dy: f32 },
    PointerAbsolute { x: f32, y: f32 },
    Button { button: u32, state: ButtonState },
    Scroll { dx: f32, dy: f32, discrete: bool },
}

enum InjectionControl {
    Inject {
        action: InjectAction,
        reply: oneshot::Sender<Result<(), String>>,
    },
    Stop {
        reply: oneshot::Sender<Result<(), String>>,
    },
}

pub struct InjectionHandle {
    control: mpsc::Sender<InjectionControl>,
    join: JoinHandle<()>,
}

impl InjectionHandle {
    /// Report whether the portal session behind this handle is still alive.
    ///
    /// The compositor destroys the RemoteDesktop session when the screen
    /// locks, which ends the task while the handle is still held. Recovering
    /// means replacing the dead handle, not refusing a second initialization.
    pub fn is_running(&self) -> bool {
        !self.join.is_finished()
    }

    pub async fn inject(&self, action: InjectAction) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(InjectionControl::Inject { action, reply })
            .await
            .map_err(|_| anyhow!("input injection task is not running"))?;
        response
            .await
            .map_err(|_| anyhow!("input injection task stopped without replying"))?
            .map_err(anyhow::Error::msg)
    }

    pub async fn stop(self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.control
            .send(InjectionControl::Stop { reply })
            .await
            .map_err(|_| anyhow!("input injection task is not running"))?;
        let result = response
            .await
            .map_err(|_| anyhow!("input injection task stopped without replying"))?
            .map_err(anyhow::Error::msg);
        let _ = self.join.await;
        result
    }
}

struct InjectionDevice {
    device: Device,
    resumed: bool,
    emulating: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct AbsoluteTarget {
    device_index: usize,
    x: f32,
    y: f32,
    distance_squared: f32,
}

pub async fn start_injection(
    restore_token: Option<String>,
    output: Output,
) -> Result<(InjectionHandle, Value)> {
    let (control, control_rx) = mpsc::channel(128);
    let (ready, ready_rx) = oneshot::channel();
    let task_output = output.clone();
    // reis keeps non-Send callback state in its event converter, so EI tasks must
    // stay on the bridge's LocalSet.
    let join = tokio::task::spawn_local(async move {
        let mut ready = Some(ready);
        if let Err(error) =
            run_injection(restore_token, task_output.clone(), control_rx, &mut ready).await
        {
            let message = format!("{error:#}");
            if let Some(ready) = ready.take() {
                let _ = ready.send(Err(message));
            } else {
                let _ = task_output
                    .send(event("inject_error", json!({ "error": message })))
                    .await;
            }
        }
    });

    let info = ready_rx
        .await
        .map_err(|_| anyhow!("input injection task stopped during initialization"))?
        .map_err(anyhow::Error::msg)?;
    Ok((InjectionHandle { control, join }, info))
}

async fn run_injection(
    restore_token: Option<String>,
    output: Output,
    mut control: mpsc::Receiver<InjectionControl>,
    ready: &mut Option<oneshot::Sender<Result<Value, String>>>,
) -> Result<()> {
    let portal = RemoteDesktop::new()
        .await
        .context("RemoteDesktop portal is unavailable")?;
    if portal.version() < 2 {
        bail!("RemoteDesktop portal version 2 or newer is required for EIS injection");
    }

    let session = portal.create_session(Default::default()).await?;
    portal
        .select_devices(
            &session,
            SelectDevicesOptions::default()
                .set_devices(DeviceType::Keyboard | DeviceType::Pointer)
                .set_restore_token(restore_token.as_deref())
                .set_persist_mode(PersistMode::ExplicitlyRevoked),
        )
        .await?
        .response()?;
    let selected = portal
        .start(&session, None, StartOptions::default())
        .await?
        .response()?;
    let selected_devices = selected.devices();
    let returned_restore_token = selected.restore_token().map(ToOwned::to_owned);

    let fd = portal
        .connect_to_eis(&session, ConnectToEISOptions::default())
        .await
        .context("failed to connect RemoteDesktop to EIS")?;
    let stream = UnixStream::from(fd);
    stream.set_nonblocking(true)?;
    let context = ei::Context::new(stream)?;
    let (connection, mut events) = context
        .handshake_tokio(
            "mwb-portal-bridge-inject",
            ei::handshake::ContextType::Sender,
        )
        .await
        .context("RemoteDesktop EI handshake failed")?;

    let mut devices = Vec::new();
    let mut next_sequence = 1;
    tokio::time::timeout(Duration::from_secs(5), async {
        while devices.is_empty() {
            let event_value = events
                .next()
                .await
                .ok_or_else(|| anyhow!("RemoteDesktop EIS connection closed during setup"))??;
            process_device_event(
                event_value,
                &connection,
                &mut devices,
                &mut next_sequence,
                &output,
            )
            .await?;
        }
        Ok::<(), anyhow::Error>(())
    })
    .await
    .context("timed out waiting for an EIS injection device")??;

    let info = json!({
        "portal_version": portal.version(),
        "selected_devices": format!("{selected_devices:?}"),
        "restore_token": returned_restore_token,
        "capabilities": capability_summary(&devices),
    });
    ready
        .take()
        .expect("ready result is sent once")
        .send(Ok(info))
        .map_err(|_| anyhow!("command reader stopped during input injection initialization"))?;

    loop {
        tokio::select! {
            command = control.recv() => {
                let Some(command) = command else {
                    close_injection(&session, &connection, &mut devices).await?;
                    break;
                };
                match command {
                    InjectionControl::Inject { action, reply } => {
                        let result = inject_action(action, &connection, &devices)
                            .map_err(|error| format!("{error:#}"));
                        let _ = reply.send(result);
                    }
                    InjectionControl::Stop { reply } => {
                        let result = close_injection(&session, &connection, &mut devices)
                            .await
                            .map_err(|error| format!("{error:#}"));
                        let _ = reply.send(result);
                        break;
                    }
                }
            }
            event_value = events.next() => {
                let Some(event_value) = event_value else {
                    bail!("RemoteDesktop EIS connection closed");
                };
                process_device_event(
                    event_value?,
                    &connection,
                    &mut devices,
                    &mut next_sequence,
                    &output,
                ).await?;
            }
        }
    }

    Ok(())
}

async fn process_device_event(
    event_value: EiEvent,
    connection: &Connection,
    devices: &mut Vec<InjectionDevice>,
    next_sequence: &mut u32,
    output: &Output,
) -> Result<()> {
    match event_value {
        EiEvent::SeatAdded(event_value) => {
            event_value.seat.bind_capabilities(&[
                DeviceCapability::Pointer,
                DeviceCapability::PointerAbsolute,
                DeviceCapability::Keyboard,
                DeviceCapability::Scroll,
                DeviceCapability::Button,
            ]);
            connection.flush()?;
        }
        EiEvent::DeviceAdded(event_value) => {
            event_value
                .device
                .device()
                .start_emulating(connection.serial(), *next_sequence);
            *next_sequence = next_sequence.wrapping_add(1).max(1);
            connection.flush()?;
            output
                .send(event(
                    "inject_device_added",
                    json!({
                        "name": event_value.device.name(),
                        "keyboard": event_value.device.has_capability(DeviceCapability::Keyboard),
                        "pointer": event_value.device.has_capability(DeviceCapability::Pointer),
                        "pointer_absolute": event_value.device.has_capability(DeviceCapability::PointerAbsolute),
                        "button": event_value.device.has_capability(DeviceCapability::Button),
                        "scroll": event_value.device.has_capability(DeviceCapability::Scroll),
                        "regions": event_value.device.regions().iter().map(|region| json!({
                            "x": region.x,
                            "y": region.y,
                            "width": region.width,
                            "height": region.height,
                            "scale": region.scale,
                            "mapping_id": region.mapping_id,
                        })).collect::<Vec<_>>(),
                    }),
                ))
                .await
                .map_err(|_| anyhow!("stdout writer stopped"))?;
            devices.push(InjectionDevice {
                device: event_value.device,
                resumed: true,
                emulating: true,
            });
        }
        EiEvent::DeviceResumed(event_value) => {
            if let Some(device) = devices
                .iter_mut()
                .find(|device| device.device == event_value.device)
            {
                device.resumed = true;
            }
            output
                .send(event(
                    "inject_devices_resumed",
                    json!({ "active": active_device_count(devices) }),
                ))
                .await
                .map_err(|_| anyhow!("stdout writer stopped"))?;
        }
        EiEvent::DevicePaused(event_value) => {
            if let Some(device) = devices
                .iter_mut()
                .find(|device| device.device == event_value.device)
            {
                device.resumed = false;
            }
            // The compositor pauses every injection device while the session
            // is locked. Reporting it turns silent dead input into a status
            // the user can act on.
            output
                .send(event(
                    "inject_devices_paused",
                    json!({ "active": active_device_count(devices) }),
                ))
                .await
                .map_err(|_| anyhow!("stdout writer stopped"))?;
        }
        EiEvent::DeviceRemoved(event_value) => {
            devices.retain(|device| device.device != event_value.device);
            output
                .send(event("inject_device_removed", json!({})))
                .await
                .map_err(|_| anyhow!("stdout writer stopped"))?;
        }
        EiEvent::Disconnected(event_value) => {
            bail!("EIS disconnected: {}", event_value.explanation)
        }
        _ => {}
    }
    Ok(())
}

fn inject_action(
    action: InjectAction,
    connection: &Connection,
    devices: &[InjectionDevice],
) -> Result<()> {
    match action {
        InjectAction::Key { keycode, state } => {
            let (device, keyboard) = devices
                .iter()
                .filter(|device| device.resumed)
                .find_map(|device| {
                    device
                        .device
                        .interface::<ei::Keyboard>()
                        .map(|interface| (device, interface))
                })
                .ok_or_else(|| anyhow!("no active EIS keyboard device is available"))?;
            keyboard.key(
                keycode,
                match state {
                    KeyState::Pressed => keyboard::KeyState::Press,
                    KeyState::Released => keyboard::KeyState::Released,
                },
            );
            finish_frame(device, connection)?;
        }
        InjectAction::PointerMotion { dx, dy } => {
            ensure_finite(&[dx, dy])?;
            let (device, pointer) = devices
                .iter()
                .filter(|device| device.resumed)
                .find_map(|device| {
                    device
                        .device
                        .interface::<ei::Pointer>()
                        .map(|interface| (device, interface))
                })
                .ok_or_else(|| anyhow!("no active EIS relative pointer device is available"))?;
            pointer.motion_relative(dx, dy);
            finish_frame(device, connection)?;
        }
        InjectAction::PointerAbsolute { x, y } => {
            ensure_finite(&[x, y])?;
            let target = closest_absolute_region(
                devices
                    .iter()
                    .enumerate()
                    .filter(|(_, device)| {
                        device.resumed
                            && device
                                .device
                                .has_capability(DeviceCapability::PointerAbsolute)
                    })
                    .flat_map(|(device_index, device)| {
                        device.device.regions().iter().map(move |region| {
                            (
                                device_index,
                                [region.x, region.y, region.width, region.height],
                            )
                        })
                    }),
                x,
                y,
            )
            .ok_or_else(|| anyhow!("no active EIS absolute pointer region is available"))?;
            let device = &devices[target.device_index];
            let pointer = device
                .device
                .interface::<ei::PointerAbsolute>()
                .ok_or_else(|| anyhow!("selected EIS device has no absolute pointer interface"))?;
            pointer.motion_absolute(target.x, target.y);
            finish_frame(device, connection)?;
        }
        InjectAction::Button { button, state } => {
            let (device, button_interface) = devices
                .iter()
                .filter(|device| device.resumed)
                .find_map(|device| {
                    device
                        .device
                        .interface::<ei::Button>()
                        .map(|interface| (device, interface))
                })
                .ok_or_else(|| anyhow!("no active EIS pointer button device is available"))?;
            button_interface.button(
                button,
                match state {
                    ButtonState::Pressed => button::ButtonState::Press,
                    ButtonState::Released => button::ButtonState::Released,
                },
            );
            finish_frame(device, connection)?;
        }
        InjectAction::Scroll { dx, dy, discrete } => {
            ensure_finite(&[dx, dy])?;
            let (device, scroll) = devices
                .iter()
                .filter(|device| device.resumed)
                .find_map(|device| {
                    device
                        .device
                        .interface::<ei::Scroll>()
                        .map(|interface| (device, interface))
                })
                .ok_or_else(|| anyhow!("no active EIS scroll device is available"))?;
            if discrete {
                scroll.scroll_discrete(float_to_i32(dx)?, float_to_i32(dy)?);
            } else {
                scroll.scroll(dx, dy);
            }
            finish_frame(device, connection)?;
        }
    }
    Ok(())
}

fn finish_frame(device: &InjectionDevice, connection: &Connection) -> Result<()> {
    device
        .device
        .device()
        .frame(connection.serial(), monotonic_time_us());
    connection.flush()?;
    Ok(())
}

async fn close_injection(
    session: &ashpd::desktop::Session<RemoteDesktop>,
    connection: &Connection,
    devices: &mut [InjectionDevice],
) -> Result<()> {
    for device in devices.iter_mut().filter(|device| device.emulating) {
        device.device.device().stop_emulating(connection.serial());
        device.emulating = false;
    }
    connection.flush()?;
    session.close().await?;
    Ok(())
}

fn active_device_count(devices: &[InjectionDevice]) -> usize {
    devices.iter().filter(|device| device.resumed).count()
}

fn capability_summary(devices: &[InjectionDevice]) -> Value {
    json!({
        "keyboard": devices.iter().any(|device| device.device.has_capability(DeviceCapability::Keyboard)),
        "pointer": devices.iter().any(|device| device.device.has_capability(DeviceCapability::Pointer)),
        "pointer_absolute": devices.iter().any(|device| device.device.has_capability(DeviceCapability::PointerAbsolute)),
        "button": devices.iter().any(|device| device.device.has_capability(DeviceCapability::Button)),
        "scroll": devices.iter().any(|device| device.device.has_capability(DeviceCapability::Scroll)),
    })
}

fn closest_absolute_region(
    regions: impl IntoIterator<Item = (usize, [u32; 4])>,
    x: f32,
    y: f32,
) -> Option<AbsoluteTarget> {
    regions
        .into_iter()
        .filter_map(|(device_index, [region_x, region_y, width, height])| {
            if width == 0 || height == 0 {
                return None;
            }
            let left = region_x as f32;
            let top = region_y as f32;
            let right = region_x.saturating_add(width - 1) as f32;
            let bottom = region_y.saturating_add(height - 1) as f32;
            let clamped_x = x.clamp(left, right);
            let clamped_y = y.clamp(top, bottom);
            Some(AbsoluteTarget {
                device_index,
                x: clamped_x,
                y: clamped_y,
                distance_squared: (x - clamped_x).powi(2) + (y - clamped_y).powi(2),
            })
        })
        .min_by(|left, right| left.distance_squared.total_cmp(&right.distance_squared))
}

fn ensure_finite(values: &[f32]) -> Result<()> {
    if values.iter().all(|value| value.is_finite()) {
        Ok(())
    } else {
        bail!("injected coordinates must be finite")
    }
}

fn float_to_i32(value: f32) -> Result<i32> {
    if !value.is_finite() || value < i32::MIN as f32 || value > i32::MAX as f32 {
        bail!("discrete scroll value is outside the i32 range");
    }
    Ok(value.round() as i32)
}

fn monotonic_time_us() -> u64 {
    let now = clock_gettime(ClockId::Monotonic);
    (now.tv_sec as u64)
        .saturating_mul(1_000_000)
        .saturating_add((now.tv_nsec as u64) / 1_000)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discrete_scroll_rounds_to_protocol_units() {
        assert_eq!(float_to_i32(119.6).unwrap(), 120);
        assert_eq!(float_to_i32(-120.0).unwrap(), -120);
    }

    #[test]
    fn invalid_motion_values_are_rejected() {
        assert!(ensure_finite(&[f32::NAN, 1.0]).is_err());
        assert!(ensure_finite(&[0.0, f32::INFINITY]).is_err());
    }

    #[test]
    fn absolute_pointer_selects_across_devices_and_clamps_outer_endpoints() {
        let regions = [(0, [0, 0, 1920, 1080]), (1, [1920, 0, 2560, 1440])];

        assert_eq!(
            closest_absolute_region(regions, 1919.0, 1079.0),
            Some(AbsoluteTarget {
                device_index: 0,
                x: 1919.0,
                y: 1079.0,
                distance_squared: 0.0,
            })
        );
        assert_eq!(
            closest_absolute_region(regions, 4480.0, 1440.0),
            Some(AbsoluteTarget {
                device_index: 1,
                x: 4479.0,
                y: 1439.0,
                distance_squared: 2.0,
            })
        );
    }

    #[test]
    fn absolute_pointer_uses_region_containing_shared_monitor_boundary() {
        let regions = [(0, [0, 0, 1920, 1080]), (1, [1920, 0, 2560, 1440])];

        let target = closest_absolute_region(regions, 1920.0, 600.0).unwrap();

        assert_eq!(target.device_index, 1);
        assert_eq!((target.x, target.y), (1920.0, 600.0));
    }

    #[test]
    fn monotonic_timestamp_is_nonzero() {
        assert!(monotonic_time_us() > 0);
    }
}
