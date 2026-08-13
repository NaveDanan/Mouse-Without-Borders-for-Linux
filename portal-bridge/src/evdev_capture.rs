// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

//! Screen-edge capture driven by `/dev/input` instead of the portal.
//!
//! Mirrors [`crate::capture`]'s control surface and emits the same events, so
//! the daemon does not care which backend is running. The difference is that
//! nothing here belongs to a compositor session: there is no consent prompt,
//! and a lock screen cannot take it away.
//!
//! Grabbing real input devices is the one operation in this program that can
//! lock a user out of their own machine, so release is unconditional. Every
//! exit path drops the devices, [`crate::evdev::InputDevice`] releases on
//! drop, and a watchdog releases a capture that has lasted implausibly long
//! without the daemon asking for it back.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Result, anyhow, bail};
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::capture::CaptureTargetSpec;
use crate::evdev::{
    BTN_FIRST, Bounds, DeviceKind, EV_KEY, EV_REL, EdgeTracker, InputDevice, REL_HWHEEL, REL_WHEEL,
    REL_X, REL_Y, button_code, discover, wheel_to_scroll,
};
use crate::protocol::{Edge, event};

type Output = mpsc::Sender<Value>;

/// How long a capture may run without the daemon releasing it.
///
/// Reaching this means the daemon stopped talking to us, and holding the
/// user's keyboard hostage is far worse than ending the capture early.
const CAPTURE_WATCHDOG: Duration = Duration::from_secs(180);

/// How often the reader wakes to check for shutdown while idle.
const POLL_INTERVAL: Duration = Duration::from_millis(150);

/// How often the device list is re-read while capture is idle.
///
/// A mouse plugged in mid-session has to be captured too, or it would keep
/// driving the local cursor while the remote computer is being controlled.
const RESCAN_INTERVAL: Duration = Duration::from_secs(3);

pub enum EvdevControl {
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

pub struct EvdevCaptureHandle {
    control: mpsc::Sender<EvdevControl>,
    join: JoinHandle<()>,
}

impl EvdevCaptureHandle {
    pub fn is_running(&self) -> bool {
        !self.join.is_finished()
    }

    async fn send(
        &self,
        message: EvdevControl,
        response: oneshot::Receiver<Result<(), String>>,
    ) -> Result<()> {
        self.control
            .send(message)
            .await
            .map_err(|_| anyhow!("screen-edge capture is not running"))?;
        response
            .await
            .map_err(|_| anyhow!("screen-edge capture stopped without replying"))?
            .map_err(anyhow::Error::msg)
    }

    pub async fn release(&self, cursor_position: Option<[f64; 2]>) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.send(
            EvdevControl::Release {
                cursor_position,
                reply,
            },
            response,
        )
        .await
    }

    pub async fn enable(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.send(EvdevControl::Enable { reply }, response).await
    }

    pub async fn disable(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.send(EvdevControl::Disable { reply }, response).await
    }

    pub async fn stop(self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        let result = self.send(EvdevControl::Stop { reply }, response).await;
        let _ = self.join.await;
        result
    }
}

/// Which edges lead to another machine, and what that machine is called.
#[derive(Clone, Debug)]
pub struct Route {
    pub edge: Edge,
    pub target: Option<String>,
}

pub fn routes_from(targets: &[CaptureTargetSpec]) -> Vec<Route> {
    targets
        .iter()
        .map(|target| Route {
            edge: target.edge,
            target: target.target.clone(),
        })
        .collect()
}

/// Start reading input devices and watching for an edge crossing.
pub async fn start_evdev_capture(
    targets: Vec<CaptureTargetSpec>,
    bounds: Bounds,
    device_directory: PathBuf,
    output: Output,
) -> Result<(EvdevCaptureHandle, Value)> {
    let routes = routes_from(&targets);
    if routes.is_empty() {
        bail!("screen-edge capture needs at least one target");
    }
    // Probe before reporting success so a permission problem surfaces as a
    // failed initialization the daemon can fall back from.
    let devices = discover(&device_directory)?;
    let described: Vec<Value> = devices
        .iter()
        .map(|device| {
            json!({
                "name": device.name,
                "path": device.path.display().to_string(),
                "kind": format!("{:?}", device.kind),
            })
        })
        .collect();
    let trackable = devices
        .iter()
        .filter(|device| device.kind == DeviceKind::Pointer)
        .count();
    if trackable == 0 {
        bail!("no relative pointer device was found to detect a screen edge with");
    }

    let info = json!({
        "backend": "evdev",
        "edge": routes.first().map(|route| route.edge),
        "devices": described,
        "zones": [{
            "x": bounds.x,
            "y": bounds.y,
            "width": bounds.width,
            "height": bounds.height,
        }],
        // evdev capture is not a portal session, so it is never revoked and
        // needs no restore token.
        "restore_token": Value::Null,
        "persistent": true,
    });

    let (control_tx, control_rx) = mpsc::channel(16);
    let join = tokio::task::spawn_local(async move {
        if let Err(error) = run_capture(
            devices,
            routes,
            bounds,
            device_directory,
            output.clone(),
            control_rx,
        )
        .await
        {
            let _ = output
                .send(event(
                    "capture_error",
                    json!({ "error": format!("{error:#}") }),
                ))
                .await;
        }
    });
    Ok((
        EvdevCaptureHandle {
            control: control_tx,
            join,
        },
        info,
    ))
}

/// A device event carried from a reader thread to the capture task.
struct DeviceEvent {
    /// Which device set produced this. Events still in flight from a replaced
    /// set would otherwise be matched against the wrong device.
    generation: u64,
    index: usize,
    event_type: u16,
    code: u16,
    value: i32,
}

/// The reader threads watching one generation of the device set.
struct Readers {
    stop: Arc<AtomicBool>,
    handles: Vec<std::thread::JoinHandle<()>>,
}

impl Readers {
    fn shut_down(self) {
        self.stop.store(true, Ordering::SeqCst);
        for handle in self.handles {
            let _ = handle.join();
        }
    }
}

/// Describe a device set so two scans can be compared.
fn fingerprint(devices: &[InputDevice]) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = devices.iter().map(|device| device.path.clone()).collect();
    paths.sort();
    paths
}

async fn run_capture(
    devices: Vec<InputDevice>,
    routes: Vec<Route>,
    bounds: Bounds,
    device_directory: PathBuf,
    output: Output,
    mut control: mpsc::Receiver<EvdevControl>,
) -> Result<()> {
    let mut tracker = EdgeTracker::new(bounds);
    let mut enabled = true;
    let mut active: Option<Instant> = None;
    let mut activation_id: u32 = 0;
    let enabled_edges: Vec<Edge> = routes.iter().map(|route| route.edge).collect();

    let (events_tx, mut events_rx) = mpsc::channel::<DeviceEvent>(1024);
    let mut generation = 0u64;
    let readers = spawn_readers(&devices, events_tx.clone(), generation);

    let mut state = CaptureState {
        devices,
        readers,
        generation,
        events_tx,
    };
    let result = capture_loop(
        &mut state,
        &routes,
        &enabled_edges,
        &mut tracker,
        &mut enabled,
        &mut active,
        &mut activation_id,
        &output,
        &mut control,
        &mut events_rx,
        &device_directory,
    )
    .await;

    // Whatever happened, the user gets their devices back.
    generation = state.generation;
    let _ = generation;
    for device in state.devices.iter_mut() {
        device.release();
    }
    state.readers.shut_down();
    result
}

/// The device set currently being watched, and the threads reading it.
struct CaptureState {
    devices: Vec<InputDevice>,
    readers: Readers,
    generation: u64,
    events_tx: mpsc::Sender<DeviceEvent>,
}

impl CaptureState {
    /// Swap in a freshly discovered device set.
    ///
    /// Only ever called while idle: replacing devices mid-capture would drop
    /// grabs and strand the pointer on the remote computer.
    fn replace(&mut self, devices: Vec<InputDevice>) {
        let previous = std::mem::replace(
            &mut self.readers,
            Readers {
                stop: Arc::new(AtomicBool::new(false)),
                handles: Vec::new(),
            },
        );
        previous.shut_down();
        for device in self.devices.iter_mut() {
            device.release();
        }
        self.devices = devices;
        self.generation = self.generation.wrapping_add(1);
        self.readers = spawn_readers(&self.devices, self.events_tx.clone(), self.generation);
    }
}

fn spawn_readers(
    devices: &[InputDevice],
    events: mpsc::Sender<DeviceEvent>,
    generation: u64,
) -> Readers {
    let stop = Arc::new(AtomicBool::new(false));
    let mut handles = Vec::new();
    for (index, device) in devices.iter().enumerate() {
        let fd = device.as_raw_fd();
        let events = events.clone();
        let stop = Arc::clone(&stop);
        let name = device.name.clone();
        handles.push(std::thread::spawn(move || {
            reader_loop(index, generation, fd, &name, &events, &stop);
        }));
    }
    Readers { stop, handles }
}

/// Poll one device and forward whatever it reports.
///
/// The descriptor belongs to the capture task, which outlives every reader,
/// and the loop exits as soon as `stop` is set.
fn reader_loop(
    index: usize,
    generation: u64,
    fd: std::os::fd::RawFd,
    name: &str,
    events: &mpsc::Sender<DeviceEvent>,
    stop: &AtomicBool,
) {
    let mut buffer = [0u8; crate::evdev::EVENT_SIZE * 32];
    while !stop.load(Ordering::SeqCst) {
        let mut poll_fd = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: a single valid descriptor with a bounded timeout.
        let ready = unsafe { libc::poll(&mut poll_fd, 1, POLL_INTERVAL.as_millis() as i32) };
        if ready <= 0 {
            continue;
        }
        // SAFETY: the descriptor is readable and the buffer is owned here.
        let read =
            unsafe { libc::read(fd, buffer.as_mut_ptr() as *mut libc::c_void, buffer.len()) };
        if read <= 0 {
            continue;
        }
        for chunk in buffer[..read as usize].chunks_exact(crate::evdev::EVENT_SIZE) {
            let Some((event_type, code, value)) = crate::evdev::parse_event(chunk) else {
                continue;
            };
            if events
                .blocking_send(DeviceEvent {
                    generation,
                    index,
                    event_type,
                    code,
                    value,
                })
                .is_err()
            {
                return;
            }
        }
    }
    let _ = name;
}

#[allow(clippy::too_many_arguments)]
async fn capture_loop(
    state: &mut CaptureState,
    routes: &[Route],
    enabled_edges: &[Edge],
    tracker: &mut EdgeTracker,
    enabled: &mut bool,
    active: &mut Option<Instant>,
    activation_id: &mut u32,
    output: &Output,
    control: &mut mpsc::Receiver<EvdevControl>,
    events: &mut mpsc::Receiver<DeviceEvent>,
    device_directory: &Path,
) -> Result<()> {
    let mut watchdog = tokio::time::interval(Duration::from_secs(5));
    let mut rescan = tokio::time::interval(RESCAN_INTERVAL);
    loop {
        tokio::select! {
            // Release must always win over forwarding: a control message stuck
            // behind an event flood is a keyboard the user cannot get back.
            biased;
            message = control.recv() => {
                let Some(message) = message else { return Ok(()) };
                match message {
                    EvdevControl::Release { cursor_position, reply } => {
                        release_devices(&mut state.devices, active, tracker, cursor_position);
                        let _ = reply.send(Ok(()));
                    }
                    EvdevControl::Enable { reply } => {
                        *enabled = true;
                        let _ = reply.send(Ok(()));
                    }
                    EvdevControl::Disable { reply } => {
                        *enabled = false;
                        release_devices(&mut state.devices, active, tracker, None);
                        let _ = reply.send(Ok(()));
                        output.send(event("capture_disabled", json!({}))).await.ok();
                    }
                    EvdevControl::Stop { reply } => {
                        release_devices(&mut state.devices, active, tracker, None);
                        let _ = reply.send(Ok(()));
                        return Ok(());
                    }
                }
            }
            _ = watchdog.tick() => {
                if let Some(started) = *active
                    && started.elapsed() > CAPTURE_WATCHDOG
                {
                    release_devices(&mut state.devices, active, tracker, None);
                    output.send(event("capture_deactivated", json!({
                        "activation_id": *activation_id,
                        "reason": "watchdog",
                    }))).await.ok();
                }
            }
            _ = rescan.tick() => {
                // Only while idle: swapping devices mid-capture would drop the
                // grabs and strand the pointer on the remote computer.
                if active.is_none()
                    && let Ok(found) = discover(device_directory)
                    && fingerprint(&found) != fingerprint(&state.devices)
                {
                    let names: Vec<String> =
                        found.iter().map(|device| device.name.clone()).collect();
                    state.replace(found);
                    output
                        .send(event("capture_devices_changed", json!({ "devices": names })))
                        .await
                        .ok();
                }
            }
            device_event = events.recv() => {
                let Some(device_event) = device_event else {
                    bail!("every input device stopped reporting");
                };
                if device_event.generation != state.generation {
                    // Left over from a device set that has been replaced.
                    continue;
                }
                handle_device_event(
                    device_event, &mut state.devices, routes, enabled_edges, tracker, *enabled,
                    active, activation_id, output,
                ).await?;
            }
        }
    }
}

fn release_devices(
    devices: &mut [InputDevice],
    active: &mut Option<Instant>,
    tracker: &mut EdgeTracker,
    cursor_position: Option<[f64; 2]>,
) {
    for device in devices.iter_mut() {
        device.release();
    }
    if active.take().is_some()
        && let Some([x, y]) = cursor_position
    {
        // Put the estimate where the daemon says the cursor re-entered, so the
        // next crossing starts from the truth rather than from the edge.
        tracker.reset_to(x as i32, y as i32);
    } else {
        let (x, y) = tracker.position();
        tracker.reset_to(x, y);
    }
}

#[allow(clippy::too_many_arguments)]
async fn handle_device_event(
    device_event: DeviceEvent,
    devices: &mut [InputDevice],
    routes: &[Route],
    enabled_edges: &[Edge],
    tracker: &mut EdgeTracker,
    enabled: bool,
    active: &mut Option<Instant>,
    activation_id: &mut u32,
    output: &Output,
) -> Result<()> {
    let DeviceEvent {
        index,
        event_type,
        code,
        value,
        ..
    } = device_event;
    let capturing = active.is_some();

    if capturing {
        if let Some(payload) = forward(event_type, code, value) {
            output
                .send(payload)
                .await
                .map_err(|_| anyhow!("stdout writer stopped"))?;
        }
        return Ok(());
    }

    if !enabled || event_type != EV_REL {
        return Ok(());
    }
    // Only relative pointers drive edge detection; a touchpad's absolute
    // coordinates are not comparable and would corrupt the estimate.
    if devices.get(index).map(|device| device.kind) != Some(DeviceKind::Pointer) {
        return Ok(());
    }
    let (dx, dy) = match code {
        REL_X => (value, 0),
        REL_Y => (0, value),
        _ => return Ok(()),
    };
    let Some(crossing) = tracker.motion(dx, dy, enabled_edges) else {
        return Ok(());
    };
    let route = routes.iter().find(|route| route.edge == crossing.edge);

    for device in devices.iter_mut() {
        device.grab()?;
    }
    *active = Some(Instant::now());
    *activation_id = activation_id.wrapping_add(1);
    let (x, y) = tracker.position();
    output
        .send(event(
            "capture_activated",
            json!({
                "activation_id": *activation_id,
                "barrier_id": 0,
                "edge": crossing.edge,
                "target": route.and_then(|route| route.target.as_deref()),
                "cursor_position": [x as f64, y as f64],
            }),
        ))
        .await
        .map_err(|_| anyhow!("stdout writer stopped"))?;
    Ok(())
}

/// Translate a captured device event into the daemon's wire event.
pub fn forward(event_type: u16, code: u16, value: i32) -> Option<Value> {
    match event_type {
        EV_REL => match code {
            REL_X => Some(event("pointer_motion", json!({ "dx": value, "dy": 0 }))),
            REL_Y => Some(event("pointer_motion", json!({ "dx": 0, "dy": value }))),
            REL_WHEEL => Some(event(
                "scroll",
                json!({ "dx": 0.0, "dy": -wheel_to_scroll(value), "discrete": true }),
            )),
            REL_HWHEEL => Some(event(
                "scroll",
                json!({ "dx": wheel_to_scroll(value), "dy": 0.0, "discrete": true }),
            )),
            _ => None,
        },
        EV_KEY => {
            // evdev repeats held keys with value 2; the daemon tracks state
            // itself and a repeat would desynchronise it.
            if value != 0 && value != 1 {
                return None;
            }
            let state = if value == 1 { "pressed" } else { "released" };
            if let Some(button) = button_code(code) {
                return Some(event("button", json!({ "button": button, "state": state })));
            }
            if code >= BTN_FIRST {
                // BTN_TOUCH, BTN_TOOL_FINGER and the other tool codes a
                // touchpad reports are not keys. Forwarding them would type
                // nonsense on the remote computer.
                return None;
            }
            Some(event("key", json!({ "keycode": code, "state": state })))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Return the event name and body of a forwarded event.
    fn payload(value: Value) -> (String, serde_json::Map<String, Value>) {
        let name = value.get("event").unwrap().as_str().unwrap().to_string();
        (name, value.as_object().unwrap().clone())
    }

    #[test]
    fn pointer_motion_is_split_per_axis() {
        let (name, body) = payload(forward(EV_REL, REL_X, -4).unwrap());
        assert_eq!(name, "pointer_motion");
        assert_eq!(body["dx"], -4);
        assert_eq!(body["dy"], 0);
    }

    #[test]
    fn a_wheel_click_scrolls_in_the_daemon_direction() {
        let (name, body) = payload(forward(EV_REL, REL_WHEEL, 1).unwrap());
        assert_eq!(name, "scroll");
        // evdev counts a click up as +1; the daemon uses the Windows sign.
        assert_eq!(body["dy"], -120.0);
        assert_eq!(body["discrete"], true);
    }

    #[test]
    fn pointer_buttons_and_keys_are_told_apart() {
        let (name, body) = payload(forward(EV_KEY, 0x110, 1).unwrap());
        assert_eq!(name, "button");
        assert_eq!(body["button"], 0x110);
        assert_eq!(body["state"], "pressed");

        let (name, body) = payload(forward(EV_KEY, 30, 0).unwrap());
        assert_eq!(name, "key");
        assert_eq!(body["keycode"], 30);
        assert_eq!(body["state"], "released");
    }

    #[test]
    fn touchpad_tool_codes_are_never_sent_as_keystrokes() {
        // BTN_TOUCH and BTN_TOOL_FINGER arrive constantly from a captured
        // touchpad; as "key" events they would type garbage remotely.
        for code in [330u16, 325, 333, 0x100, 0x11f, 0x140] {
            assert!(
                forward(EV_KEY, code, 1).is_none(),
                "code {code:#x} must not be forwarded"
            );
        }
    }

    #[test]
    fn real_mouse_buttons_survive_the_tool_code_filter() {
        for code in 0x110u16..=0x117 {
            let value = forward(EV_KEY, code, 1).expect("a mouse button must forward");
            assert_eq!(value.get("event").unwrap(), "button");
        }
    }

    #[test]
    fn key_repeats_are_dropped() {
        // Value 2 is autorepeat; forwarding it would double a held key.
        assert!(forward(EV_KEY, 30, 2).is_none());
    }

    #[test]
    fn unrelated_axes_are_ignored() {
        assert!(forward(EV_REL, 0x09, 3).is_none());
        assert!(forward(crate::evdev::EV_ABS, 0, 5).is_none());
    }

    #[test]
    fn routes_keep_their_edge_and_target() {
        let targets = vec![
            CaptureTargetSpec {
                edge: Edge::Right,
                zone: None,
                target: Some("win".into()),
            },
            CaptureTargetSpec {
                edge: Edge::Left,
                zone: None,
                target: None,
            },
        ];
        let routes = routes_from(&targets);
        assert_eq!(routes.len(), 2);
        assert_eq!(routes[0].edge, Edge::Right);
        assert_eq!(routes[0].target.as_deref(), Some("win"));
        assert_eq!(routes[1].target, None);
    }
}
