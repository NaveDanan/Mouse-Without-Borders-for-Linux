// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

use anyhow::{Result, anyhow, bail};
use mwb_portal_bridge::capture::{CaptureHandle, CaptureTargetSpec, Zone, start_capture};
use mwb_portal_bridge::evdev::Bounds;
use mwb_portal_bridge::evdev_capture::{EvdevCaptureHandle, start_evdev_capture};
use mwb_portal_bridge::inject::{InjectAction, InjectionHandle, start_injection};
use mwb_portal_bridge::protocol::{
    CaptureBackend, CaptureTarget, Command, Edge, InjectBackend, event, response_error, response_ok,
};
use mwb_portal_bridge::uinput::{UinputInjector, availability_error};
use serde_json::{Value, json};
use std::rc::Rc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{Mutex, mpsc};

struct Bridge {
    capture: Option<CaptureHandle>,
    /// Screen-edge capture read straight from the input devices. Mutually
    /// exclusive with `capture`, which is the portal path.
    evdev_capture: Option<EvdevCaptureHandle>,
    injection: Option<InjectionHandle>,
    /// Kernel-level injection, used instead of the portal when selected. It
    /// survives the lock screen, which no portal session can.
    uinput: Option<UinputInjector>,
    /// Set while a session is being created. Initialization runs off the
    /// command loop, so this is what stops a second request racing it.
    capture_initializing: bool,
    inject_initializing: bool,
    output: mpsc::Sender<Value>,
}

impl Bridge {
    fn new(output: mpsc::Sender<Value>) -> Self {
        Self {
            capture: None,
            evdev_capture: None,
            injection: None,
            uinput: None,
            capture_initializing: false,
            inject_initializing: false,
            output,
        }
    }

    async fn handle(&mut self, command: Command) -> (Value, bool) {
        let id = command.id();
        let result = self.handle_inner(command).await;
        match result {
            Ok((result, shutdown)) => (response_ok(id, result), shutdown),
            Err(error) => (response_error(id, format!("{error:#}")), false),
        }
    }

    async fn handle_inner(&mut self, command: Command) -> Result<(Value, bool)> {
        match command {
            Command::Ping { .. } => Ok((
                json!({ "pong": true, "version": env!("CARGO_PKG_VERSION") }),
                false,
            )),
            Command::CaptureInit { .. } | Command::InjectInit { .. } => {
                // Routed off the command loop by run(); reaching here means a
                // dispatch path was missed.
                Err(anyhow!("session initialization is handled asynchronously"))
            }
            Command::CaptureRelease {
                cursor_position, ..
            } => {
                if let Some(evdev) = self.evdev_capture.as_ref() {
                    evdev.release(cursor_position).await?;
                    return Ok((json!({ "released": true }), false));
                }
                self.capture
                    .as_ref()
                    .ok_or_else(|| anyhow!("input capture is not initialized"))?
                    .release(cursor_position)
                    .await?;
                Ok((json!({ "released": true }), false))
            }
            Command::CaptureEnable { .. } => {
                if let Some(evdev) = self.evdev_capture.as_ref() {
                    evdev.enable().await?;
                    return Ok((json!({ "enabled": true }), false));
                }
                self.capture
                    .as_ref()
                    .ok_or_else(|| anyhow!("input capture is not initialized"))?
                    .enable()
                    .await?;
                Ok((json!({ "enabled": true }), false))
            }
            Command::CaptureDisable { .. } => {
                if let Some(evdev) = self.evdev_capture.as_ref() {
                    evdev.disable().await?;
                    return Ok((json!({ "disabled": true }), false));
                }
                self.capture
                    .as_ref()
                    .ok_or_else(|| anyhow!("input capture is not initialized"))?
                    .disable()
                    .await?;
                Ok((json!({ "disabled": true }), false))
            }
            Command::CaptureStop { .. } => {
                if let Some(evdev) = self.evdev_capture.take() {
                    evdev.stop().await?;
                    return Ok((json!({ "stopped": true }), false));
                }
                let capture = self
                    .capture
                    .take()
                    .ok_or_else(|| anyhow!("input capture is not initialized"))?;
                capture.stop().await?;
                Ok((json!({ "stopped": true }), false))
            }
            Command::InjectKey { keycode, state, .. } => {
                self.inject(InjectAction::Key { keycode, state }).await?;
                Ok((json!({ "injected": true }), false))
            }
            Command::InjectPointerMotion { dx, dy, .. } => {
                self.inject(InjectAction::PointerMotion { dx, dy }).await?;
                Ok((json!({ "injected": true }), false))
            }
            Command::InjectPointerAbsolute { x, y, .. } => {
                self.inject(InjectAction::PointerAbsolute { x, y }).await?;
                Ok((json!({ "injected": true }), false))
            }
            Command::InjectButton { button, state, .. } => {
                self.inject(InjectAction::Button { button, state }).await?;
                Ok((json!({ "injected": true }), false))
            }
            Command::InjectScroll {
                dx, dy, discrete, ..
            } => {
                self.inject(InjectAction::Scroll { dx, dy, discrete })
                    .await?;
                Ok((json!({ "injected": true }), false))
            }
            Command::InjectStop { .. } => {
                if self.uinput.take().is_some() {
                    return Ok((json!({ "stopped": true }), false));
                }
                let injection = self
                    .injection
                    .take()
                    .ok_or_else(|| anyhow!("input injection is not initialized"))?;
                // A session the compositor already destroyed cannot be closed
                // politely, and reporting that as an error would block the
                // daemon's recovery path.
                if injection.is_running() {
                    injection.stop().await?;
                }
                Ok((json!({ "stopped": true }), false))
            }
            Command::Shutdown { .. } => {
                self.stop_all().await?;
                Ok((json!({ "shutdown": true }), true))
            }
        }
    }

    /// Drop a handle whose portal session the compositor already destroyed.
    ///
    /// Returns true when the slot is free for a fresh session.
    fn discard_dead_injection(&mut self) -> bool {
        match &self.injection {
            Some(injection) if injection.is_running() => false,
            Some(_) => {
                self.injection = None;
                true
            }
            None => true,
        }
    }

    fn discard_dead_evdev_capture(&mut self) -> bool {
        match &self.evdev_capture {
            Some(capture) if capture.is_running() => false,
            Some(_) => {
                self.evdev_capture = None;
                true
            }
            None => true,
        }
    }

    fn discard_dead_capture(&mut self) -> bool {
        match &self.capture {
            Some(capture) if capture.is_running() => false,
            Some(_) => {
                self.capture = None;
                true
            }
            None => true,
        }
    }

    async fn inject(&mut self, action: InjectAction) -> Result<()> {
        if let Some(uinput) = self.uinput.as_mut() {
            return match action {
                InjectAction::Key { keycode, state } => uinput.key(keycode, state),
                InjectAction::PointerMotion { dx, dy } => uinput.motion(dx, dy),
                InjectAction::PointerAbsolute { x, y } => uinput.absolute(x, y),
                InjectAction::Button { button, state } => uinput.button(button, state),
                InjectAction::Scroll { dx, dy, discrete } => uinput.scroll(dx, dy, discrete),
            };
        }
        self.injection
            .as_ref()
            .ok_or_else(|| anyhow!("input injection is not initialized"))?
            .inject(action)
            .await
    }

    async fn stop_all(&mut self) -> Result<()> {
        let mut errors = Vec::new();
        self.uinput = None;
        // Release grabbed input devices before anything else can fail: a
        // device left captured is a keyboard the user cannot get back.
        if let Some(evdev) = self.evdev_capture.take()
            && let Err(error) = evdev.stop().await
        {
            errors.push(format!("evdev capture: {error:#}"));
        }
        if let Some(capture) = self.capture.take()
            && let Err(error) = capture.stop().await
        {
            errors.push(format!("capture: {error:#}"));
        }
        if let Some(injection) = self.injection.take()
            && let Err(error) = injection.stop().await
        {
            errors.push(format!("injection: {error:#}"));
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(anyhow!(errors.join("; ")))
        }
    }
}

/// Create a capture session without blocking the command loop.
///
/// A portal request can wait on a consent dialog for as long as the user
/// takes. Holding the loop for that would also hold back `capture_release`,
/// which is what hands grabbed input devices back, so this runs on its own
/// task and answers when it is done.
async fn initialize_capture(bridge: Rc<Mutex<Bridge>>, command: Command) -> Result<Value> {
    let Command::CaptureInit {
        edge,
        restore_token,
        zone,
        targets,
        backend,
        screen,
        ..
    } = command
    else {
        bail!("expected a capture_init command");
    };
    let output = {
        let mut bridge = bridge.lock().await;
        if bridge.capture_initializing
            || !bridge.discard_dead_evdev_capture()
            || !bridge.discard_dead_capture()
        {
            bail!("input capture is already initialized");
        }
        bridge.capture_initializing = true;
        bridge.output.clone()
    };
    let result =
        start_capture_session(edge, restore_token, zone, targets, backend, screen, &output).await;
    let mut bridge = bridge.lock().await;
    bridge.capture_initializing = false;
    match result? {
        StartedCapture::Evdev(handle, info) => {
            bridge.evdev_capture = Some(handle);
            Ok(info)
        }
        StartedCapture::Portal(handle, info) => {
            bridge.capture = Some(handle);
            Ok(info)
        }
    }
}

enum StartedCapture {
    Evdev(EvdevCaptureHandle, Value),
    Portal(CaptureHandle, Value),
}

#[allow(clippy::too_many_arguments)]
async fn start_capture_session(
    edge: Option<Edge>,
    restore_token: Option<String>,
    zone: Option<[i32; 4]>,
    targets: Option<Vec<CaptureTarget>>,
    backend: Option<CaptureBackend>,
    screen: Option<[i32; 4]>,
    output: &mpsc::Sender<Value>,
) -> Result<StartedCapture> {
    let targets = capture_target_specs(edge, zone, targets)?;
    let backend = backend.unwrap_or_default();
    if matches!(backend, CaptureBackend::Evdev | CaptureBackend::Auto) {
        let screen = screen.ok_or_else(|| anyhow!("evdev capture needs the desktop geometry"))?;
        let bounds = Bounds::new(screen[0], screen[1], screen[2], screen[3]);
        match start_evdev_capture(
            targets.clone(),
            bounds,
            std::path::PathBuf::from("/dev/input"),
            output.clone(),
        )
        .await
        {
            Ok((handle, info)) => return Ok(StartedCapture::Evdev(handle, info)),
            Err(error) if matches!(backend, CaptureBackend::Evdev) => return Err(error),
            Err(error) => {
                output
                    .send(event(
                        "capture_backend_fallback",
                        json!({
                            "requested": "evdev",
                            "using": "portal",
                            "reason": format!("{error:#}"),
                        }),
                    ))
                    .await
                    .ok();
            }
        }
    }
    let (handle, info) = start_capture(targets, restore_token, output.clone()).await?;
    Ok(StartedCapture::Portal(handle, info))
}

/// Create an injection session without blocking the command loop.
async fn initialize_injection(bridge: Rc<Mutex<Bridge>>, command: Command) -> Result<Value> {
    let Command::InjectInit {
        restore_token,
        backend,
        screen,
        ..
    } = command
    else {
        bail!("expected an inject_init command");
    };
    let output = {
        let mut bridge = bridge.lock().await;
        if bridge.inject_initializing || bridge.uinput.is_some() || !bridge.discard_dead_injection()
        {
            bail!("input injection is already initialized");
        }
        bridge.inject_initializing = true;
        bridge.output.clone()
    };
    let backend = backend.unwrap_or_default();
    if matches!(backend, InjectBackend::Uinput | InjectBackend::Auto) {
        let screen = match screen {
            Some(screen) => screen,
            None => {
                bridge.lock().await.inject_initializing = false;
                bail!("uinput injection needs the desktop geometry");
            }
        };
        match UinputInjector::new(screen) {
            Ok(injector) => {
                let info = injector.describe();
                let mut bridge = bridge.lock().await;
                bridge.inject_initializing = false;
                bridge.uinput = Some(injector);
                return Ok(info);
            }
            Err(error) if matches!(backend, InjectBackend::Uinput) => {
                bridge.lock().await.inject_initializing = false;
                return Err(error);
            }
            Err(error) => {
                output
                    .send(event(
                        "inject_backend_fallback",
                        json!({
                            "requested": "uinput",
                            "using": "portal",
                            "reason": availability_error()
                                .unwrap_or_else(|| format!("{error:#}")),
                        }),
                    ))
                    .await
                    .ok();
            }
        }
    }
    let result = start_injection(restore_token, output.clone()).await;
    let mut bridge = bridge.lock().await;
    bridge.inject_initializing = false;
    let (handle, info) = result?;
    bridge.injection = Some(handle);
    Ok(info)
}

fn capture_target_specs(
    legacy_edge: Option<Edge>,
    legacy_zone: Option<[i32; 4]>,
    targets: Option<Vec<CaptureTarget>>,
) -> Result<Vec<CaptureTargetSpec>> {
    if let Some(targets) = targets {
        if targets.is_empty() {
            bail!("capture targets cannot be empty");
        }
        return targets
            .into_iter()
            .map(|target| {
                if target.target.trim().is_empty() {
                    bail!("capture target name cannot be empty");
                }
                Ok(CaptureTargetSpec {
                    edge: target.edge,
                    zone: target.zone.map(zone_from_wire).transpose()?,
                    target: Some(target.target),
                })
            })
            .collect();
    }

    let edge = legacy_edge.ok_or_else(|| anyhow!("capture_init requires edge or targets"))?;
    Ok(vec![CaptureTargetSpec {
        edge,
        zone: legacy_zone.map(zone_from_wire).transpose()?,
        target: None,
    }])
}

fn zone_from_wire([x, y, width, height]: [i32; 4]) -> Result<Zone> {
    if width <= 0 || height <= 0 {
        bail!("capture target zone width and height must be positive");
    }
    Ok(Zone {
        x,
        y,
        width: width as u32,
        height: height as u32,
    })
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    tokio::task::LocalSet::new().run_until(run()).await
}

async fn run() -> Result<()> {
    let (output, mut output_rx) = mpsc::channel::<Value>(1024);
    let writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(value) = output_rx.recv().await {
            let mut bytes = serde_json::to_vec(&value)?;
            bytes.push(b'\n');
            stdout.write_all(&bytes).await?;
            stdout.flush().await?;
        }
        Ok::<(), anyhow::Error>(())
    });

    let bridge = Rc::new(Mutex::new(Bridge::new(output.clone())));
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let command = match serde_json::from_str::<Command>(&line) {
            Ok(command) => command,
            Err(error) => {
                output
                    .send(response_error(None, format!("invalid command: {error}")))
                    .await
                    .map_err(|_| anyhow!("stdout writer stopped"))?;
                continue;
            }
        };
        // Session creation can wait on a consent dialog indefinitely. Running
        // it here would also hold back capture_release, which is what returns
        // grabbed input devices, so it gets its own task and replies later.
        if matches!(
            command,
            Command::CaptureInit { .. } | Command::InjectInit { .. }
        ) {
            let bridge = Rc::clone(&bridge);
            let output = output.clone();
            tokio::task::spawn_local(async move {
                let id = command.id();
                let is_capture = matches!(command, Command::CaptureInit { .. });
                let result = if is_capture {
                    initialize_capture(bridge, command).await
                } else {
                    initialize_injection(bridge, command).await
                };
                let response = match result {
                    Ok(info) => response_ok(id, info),
                    Err(error) => response_error(id, format!("{error:#}")),
                };
                let _ = output.send(response).await;
            });
            continue;
        }
        let (response, shutdown) = bridge.lock().await.handle(command).await;
        output
            .send(response)
            .await
            .map_err(|_| anyhow!("stdout writer stopped"))?;
        if shutdown {
            break;
        }
    }

    if let Err(error) = bridge.lock().await.stop_all().await {
        eprintln!("portal bridge cleanup failed: {error:#}");
    }
    drop(bridge);
    drop(output);
    writer.await??;
    Ok(())
}

#[cfg(test)]
mod tests {
    /// Session creation must never be answered on the command loop.
    ///
    /// A portal request can sit on a consent dialog indefinitely, and while it
    /// does, capture_release cannot be processed; that release is what hands
    /// grabbed keyboards and mice back to the user.
    #[tokio::test]
    async fn session_initialization_is_refused_on_the_command_loop() {
        use super::*;
        let (output, _rx) = mpsc::channel::<Value>(8);
        let mut bridge = Bridge::new(output);
        for command in [
            Command::CaptureInit {
                id: None,
                edge: Some(Edge::Right),
                restore_token: None,
                zone: None,
                targets: None,
                backend: None,
                screen: None,
            },
            Command::InjectInit {
                id: None,
                restore_token: None,
                backend: None,
                screen: None,
            },
        ] {
            let (response, shutdown) = bridge.handle(command).await;
            assert!(!shutdown);
            assert_eq!(response["ok"], false);
            let error = response["error"].as_str().unwrap();
            assert!(
                error.contains("asynchronously"),
                "unexpected refusal: {error}"
            );
        }
    }

    use super::*;

    #[test]
    fn legacy_capture_fields_become_one_unnamed_target() {
        let targets =
            capture_target_specs(Some(Edge::Right), Some([0, 0, 1920, 1080]), None).unwrap();

        assert_eq!(
            targets,
            vec![CaptureTargetSpec {
                edge: Edge::Right,
                zone: Some(Zone {
                    x: 0,
                    y: 0,
                    width: 1920,
                    height: 1080,
                }),
                target: None,
            }]
        );
    }

    #[test]
    fn multi_target_fields_take_precedence_and_preserve_routes() {
        let targets = capture_target_specs(
            Some(Edge::Right),
            Some([99, 99, 640, 480]),
            Some(vec![
                CaptureTarget {
                    edge: Edge::Left,
                    zone: None,
                    target: "alpha".into(),
                },
                CaptureTarget {
                    edge: Edge::Bottom,
                    zone: Some([0, 0, 1920, 1080]),
                    target: "beta".into(),
                },
            ]),
        )
        .unwrap();

        assert_eq!(targets[0].target.as_deref(), Some("alpha"));
        assert_eq!(targets[0].edge, Edge::Left);
        assert_eq!(targets[1].target.as_deref(), Some("beta"));
        assert_eq!(targets[1].edge, Edge::Bottom);
        assert_eq!(targets[1].zone.unwrap().height, 1080);
    }

    #[test]
    fn empty_or_missing_capture_targets_are_rejected() {
        assert!(capture_target_specs(None, None, Some(Vec::new())).is_err());
        assert!(capture_target_specs(None, None, None).is_err());
    }

    #[test]
    fn invalid_target_zone_is_rejected() {
        let target = CaptureTarget {
            edge: Edge::Top,
            zone: Some([0, 0, 1920, 0]),
            target: "alpha".into(),
        };
        assert!(capture_target_specs(None, None, Some(vec![target])).is_err());
    }

    #[test]
    fn empty_target_name_is_rejected() {
        let target = CaptureTarget {
            edge: Edge::Top,
            zone: None,
            target: "  ".into(),
        };
        assert!(capture_target_specs(None, None, Some(vec![target])).is_err());
    }
}
