// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

//! Screen-edge capture read straight from `/dev/input`.
//!
//! The InputCapture portal is accurate but it cannot survive a lock screen:
//! the compositor destroys the session, and interface version 1 has no restore
//! token, so every rebuild asks the user for permission again. Reading evdev
//! directly needs no portal session and therefore never prompts.
//!
//! The cost is that evdev reports raw device deltas, before the compositor
//! applies pointer acceleration, so a running position estimate drifts away
//! from the real cursor. Edge detection therefore does not rely on absolute
//! agreement: the estimate is clamped to the desktop exactly as the real
//! cursor is, so both end up pinned against the same edge, and a crossing is
//! reported only when an already-pinned pointer keeps pushing outward.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use anyhow::{Context as _, Result, bail};

use crate::protocol::Edge;

/// `_IOC(_IOC_READ, 'E', nr, size)`
const fn eviocg(nr: u32, size: u32) -> u64 {
    ((2u32 << 30) | (size << 16) | (0x45 << 8) | nr) as u64
}

/// `EVIOCGRAB`: take or release exclusive access to a device.
const EVIOCGRAB: u64 = 0x4004_4590;

/// One `input_event` as the kernel writes it on a 64-bit system.
pub const EVENT_SIZE: usize = 24;

/// Raw evdev codes this module cares about.
pub const EV_SYN: u16 = 0x00;
pub const EV_KEY: u16 = 0x01;
pub const EV_REL: u16 = 0x02;
pub const EV_ABS: u16 = 0x03;

pub const REL_X: u16 = 0x00;
pub const REL_Y: u16 = 0x01;
pub const REL_HWHEEL: u16 = 0x06;
pub const REL_WHEEL: u16 = 0x08;

/// Buttons live in the same code space as keys; this is where they start.
pub const BTN_MISC: u16 = 0x100;
/// Codes at or above this are not keyboard keys.
pub const BTN_FIRST: u16 = BTN_MISC;

/// Keys that only a real typing keyboard carries.
///
/// Power buttons, lid switches, hotkey arrays and the "Video Bus" all report a
/// handful of key codes. Grabbing those would take away the power button and
/// the brightness keys, so a keyboard has to prove itself with letters.
pub const KEY_A: u16 = 30;
pub const KEY_Z: u16 = 44;
pub const KEY_SPACE: u16 = 57;

/// How far past the edge the estimate must be pushed before a crossing counts.
///
/// A single stray delta at the edge should not teleport the user to another
/// machine, and pointer acceleration means one physical flick can be many
/// device units.
pub const EDGE_PUSH_THRESHOLD: i32 = 12;

/// What a device is used for, decided from the axes it advertises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceKind {
    Keyboard,
    Pointer,
    /// Advertises absolute axes: a touchpad or touchscreen. Its motion is not
    /// used for edge tracking because the deltas are not comparable.
    AbsolutePointer,
    Ignored,
}

/// Classify a device from the event types and codes it reports.
///
/// Anything advertising relative pointer motion is a pointer even when it also
/// has buttons, which is how mice present themselves. A keyboard must carry
/// letter keys, so that system button arrays are left alone.
pub fn classify(
    event_types: &[u16],
    key_codes: &[u16],
    rel_axes: &[u16],
    abs_axes: &[u16],
) -> DeviceKind {
    let has_rel_motion =
        event_types.contains(&EV_REL) && rel_axes.contains(&REL_X) && rel_axes.contains(&REL_Y);
    let has_abs_motion =
        event_types.contains(&EV_ABS) && abs_axes.contains(&0) && abs_axes.contains(&1);
    if has_rel_motion {
        return DeviceKind::Pointer;
    }
    if has_abs_motion {
        return DeviceKind::AbsolutePointer;
    }
    let types_letters = [KEY_A, KEY_Z, KEY_SPACE]
        .iter()
        .all(|code| key_codes.contains(code));
    if event_types.contains(&EV_KEY) && types_letters {
        return DeviceKind::Keyboard;
    }
    DeviceKind::Ignored
}

/// Map an evdev button code onto the code the daemon already exchanges.
///
/// The portal path forwards libei button codes, which are the evdev codes, so
/// the two backends agree without translation. Unknown codes are dropped
/// rather than guessed at.
pub fn button_code(code: u16) -> Option<u32> {
    match code {
        0x110..=0x117 => Some(u32::from(code)),
        _ => None,
    }
}

/// The desktop rectangle the pointer estimate is confined to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bounds {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

impl Bounds {
    pub fn new(x: i32, y: i32, width: i32, height: i32) -> Self {
        Self {
            x,
            y,
            width: width.max(1),
            height: height.max(1),
        }
    }

    fn left(&self) -> i32 {
        self.x
    }

    fn top(&self) -> i32 {
        self.y
    }

    fn right(&self) -> i32 {
        self.x + self.width - 1
    }

    fn bottom(&self) -> i32 {
        self.y + self.height - 1
    }
}

/// A crossing the caller should act on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Crossing {
    pub edge: Edge,
}

/// Dead-reckoned pointer position used purely to spot an edge crossing.
#[derive(Clone, Debug)]
pub struct EdgeTracker {
    bounds: Bounds,
    x: i32,
    y: i32,
    /// Accumulated outward push per edge while the estimate is already pinned.
    pressure: BTreeMap<u8, i32>,
    armed: bool,
}

impl EdgeTracker {
    pub fn new(bounds: Bounds) -> Self {
        Self {
            x: bounds.x + bounds.width / 2,
            y: bounds.y + bounds.height / 2,
            bounds,
            pressure: BTreeMap::new(),
            armed: true,
        }
    }

    pub fn position(&self) -> (i32, i32) {
        (self.x, self.y)
    }

    pub fn bounds(&self) -> Bounds {
        self.bounds
    }

    /// Re-centre after the desktop layout changed under us.
    pub fn set_bounds(&mut self, bounds: Bounds) {
        self.bounds = bounds;
        self.x = self.x.clamp(bounds.left(), bounds.right());
        self.y = self.y.clamp(bounds.top(), bounds.bottom());
        self.pressure.clear();
    }

    /// Stop reporting crossings until the pointer leaves the edge again.
    ///
    /// Called once a crossing has been handed over, so that continuing to push
    /// against the same edge does not fire repeatedly.
    pub fn disarm(&mut self) {
        self.armed = false;
        self.pressure.clear();
    }

    /// Place the estimate somewhere known, such as after a release.
    pub fn reset_to(&mut self, x: i32, y: i32) {
        self.x = x.clamp(self.bounds.left(), self.bounds.right());
        self.y = y.clamp(self.bounds.top(), self.bounds.bottom());
        self.pressure.clear();
        self.armed = true;
    }

    /// Feed a relative motion sample and report a crossing when one is due.
    ///
    /// Only the part of the delta the desktop boundary clipped counts as a
    /// push. The compositor clips the real cursor the same way, so overshoot
    /// is the one quantity both agree on despite pointer acceleration, and it
    /// naturally ignores the travel spent crossing the screen.
    ///
    /// `enabled_edges` lists the edges that lead somewhere; pushing against an
    /// edge with no machine behind it must not capture the pointer.
    pub fn motion(&mut self, dx: i32, dy: i32, enabled_edges: &[Edge]) -> Option<Crossing> {
        let desired_x = self.x.saturating_add(dx);
        let desired_y = self.y.saturating_add(dy);
        let new_x = desired_x.clamp(self.bounds.left(), self.bounds.right());
        let new_y = desired_y.clamp(self.bounds.top(), self.bounds.bottom());
        let moved = (new_x, new_y) != (self.x, self.y);
        self.x = new_x;
        self.y = new_y;

        let overshoot = |edge: Edge| -> i32 {
            match edge {
                Edge::Left => (self.bounds.left() - desired_x).max(0),
                Edge::Right => (desired_x - self.bounds.right()).max(0),
                Edge::Top => (self.bounds.top() - desired_y).max(0),
                Edge::Bottom => (desired_y - self.bounds.bottom()).max(0),
            }
        };

        let mut crossing = None;
        let mut any_pressure = false;
        for edge in enabled_edges {
            let slot = *edge as u8;
            let push = overshoot(*edge);
            if push <= 0 {
                self.pressure.remove(&slot);
                continue;
            }
            any_pressure = true;
            let total = self.pressure.entry(slot).or_insert(0);
            *total += push;
            if *total >= EDGE_PUSH_THRESHOLD && self.armed && crossing.is_none() {
                crossing = Some(Crossing { edge: *edge });
            }
        }

        if !any_pressure && moved {
            // The pointer is travelling freely again, so a later push against
            // the same edge should be honoured.
            self.armed = true;
        }
        if crossing.is_some() {
            self.disarm();
        }
        crossing
    }
}

/// Decode a kernel bitmask into the codes it has set.
pub fn decode_bits(bits: &[u8]) -> Vec<u16> {
    let mut codes = Vec::new();
    for (index, byte) in bits.iter().enumerate() {
        for bit in 0..8 {
            if byte & (1 << bit) != 0 {
                codes.push((index * 8 + bit) as u16);
            }
        }
    }
    codes
}

/// Parse one `input_event` record into its type, code and value.
pub fn parse_event(buffer: &[u8]) -> Option<(u16, u16, i32)> {
    if buffer.len() < EVENT_SIZE {
        return None;
    }
    let event_type = u16::from_ne_bytes(buffer[16..18].try_into().ok()?);
    let code = u16::from_ne_bytes(buffer[18..20].try_into().ok()?);
    let value = i32::from_ne_bytes(buffer[20..24].try_into().ok()?);
    Some((event_type, code, value))
}

fn ioctl_read(fd: RawFd, request: u64, buffer: &mut [u8]) -> Result<usize> {
    // SAFETY: `fd` is an open evdev descriptor and `buffer` is writable for
    // the length encoded into `request`.
    let read = unsafe { libc::ioctl(fd, request as libc::c_ulong, buffer.as_mut_ptr()) };
    if read < 0 {
        return Err(std::io::Error::last_os_error()).context("evdev ioctl failed");
    }
    Ok(read as usize)
}

/// An opened `/dev/input/event*` node with its capabilities probed.
pub struct InputDevice {
    pub path: PathBuf,
    pub name: String,
    pub kind: DeviceKind,
    file: File,
    grabbed: bool,
}

impl InputDevice {
    pub fn open(path: &Path) -> Result<Self> {
        // Non-blocking, so a caller polling for events is never stuck waiting
        // for a device the user simply is not touching.
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .with_context(|| format!("cannot read {}", path.display()))?;
        let fd = file.as_raw_fd();

        let mut name_buffer = [0u8; 256];
        let length = ioctl_read(fd, eviocg(0x06, 256), &mut name_buffer).unwrap_or(0);
        let name = String::from_utf8_lossy(&name_buffer[..length.saturating_sub(1).min(255)])
            .trim_matches(char::from(0))
            .to_string();

        let mut type_bits = [0u8; 4];
        ioctl_read(fd, eviocg(0x20, 4), &mut type_bits)?;
        let event_types = decode_bits(&type_bits);

        let mut key_bits = [0u8; 96];
        let key_codes = if event_types.contains(&EV_KEY) {
            ioctl_read(fd, eviocg(0x20 + u32::from(EV_KEY), 96), &mut key_bits).ok();
            decode_bits(&key_bits)
        } else {
            Vec::new()
        };

        let mut rel_bits = [0u8; 2];
        let rel_axes = if event_types.contains(&EV_REL) {
            ioctl_read(fd, eviocg(0x20 + u32::from(EV_REL), 2), &mut rel_bits).ok();
            decode_bits(&rel_bits)
        } else {
            Vec::new()
        };

        let mut abs_bits = [0u8; 8];
        let abs_axes = if event_types.contains(&EV_ABS) {
            ioctl_read(fd, eviocg(0x20 + u32::from(EV_ABS), 8), &mut abs_bits).ok();
            decode_bits(&abs_bits)
        } else {
            Vec::new()
        };

        let kind = classify(&event_types, &key_codes, &rel_axes, &abs_axes);
        Ok(Self {
            path: path.to_path_buf(),
            name,
            kind,
            file,
            grabbed: false,
        })
    }

    pub fn as_raw_fd(&self) -> RawFd {
        self.file.as_raw_fd()
    }

    pub fn is_grabbed(&self) -> bool {
        self.grabbed
    }

    /// Take exclusive access so local input stops reaching the compositor.
    pub fn grab(&mut self) -> Result<()> {
        if self.grabbed {
            return Ok(());
        }
        // SAFETY: `fd` is an open evdev descriptor.
        let result = unsafe { libc::ioctl(self.as_raw_fd(), EVIOCGRAB as libc::c_ulong, 1) };
        if result < 0 {
            return Err(std::io::Error::last_os_error())
                .with_context(|| format!("cannot capture {}", self.name));
        }
        self.grabbed = true;
        Ok(())
    }

    /// Hand the device back to the desktop.
    ///
    /// This must succeed on every path out of capture; a device left grabbed
    /// is a keyboard and mouse the user cannot get back.
    pub fn release(&mut self) {
        if !self.grabbed {
            return;
        }
        // SAFETY: `fd` is an open evdev descriptor.
        unsafe {
            libc::ioctl(self.as_raw_fd(), EVIOCGRAB as libc::c_ulong, 0);
        }
        self.grabbed = false;
    }

    /// Read whatever complete events are pending.
    pub fn read_events(&mut self) -> Result<Vec<(u16, u16, i32)>> {
        let mut buffer = [0u8; EVENT_SIZE * 32];
        let read = match self.file.read(&mut buffer) {
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(Vec::new()),
            Err(error) => return Err(error).context("cannot read input events"),
        };
        if read == 0 {
            bail!("{} disappeared", self.name);
        }
        Ok(buffer[..read]
            .chunks_exact(EVENT_SIZE)
            .filter_map(parse_event)
            .collect())
    }
}

impl Drop for InputDevice {
    fn drop(&mut self) {
        // Never leave a grabbed device behind, whatever unwound us.
        self.release();
    }
}

/// Find the devices worth watching for screen-edge capture.
pub fn discover(directory: &Path) -> Result<Vec<InputDevice>> {
    let mut devices = Vec::new();
    let entries = std::fs::read_dir(directory)
        .with_context(|| format!("cannot list {}", directory.display()))?;
    let mut paths: Vec<PathBuf> = entries
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("event"))
        })
        .collect();
    paths.sort();
    for path in paths {
        match InputDevice::open(&path) {
            Ok(device)
                if matches!(
                    device.kind,
                    DeviceKind::Keyboard | DeviceKind::Pointer | DeviceKind::AbsolutePointer
                ) =>
            {
                // Never capture the devices this application itself created,
                // or releasing the pointer would feed our own injection back.
                if device.name.starts_with("Mouse Without Borders virtual") {
                    continue;
                }
                devices.push(device);
            }
            Ok(_) => {}
            Err(_) => {}
        }
    }
    if devices.is_empty() {
        bail!(
            "no readable keyboard or pointer devices were found in {}",
            directory.display()
        );
    }
    Ok(devices)
}

/// Convert an evdev wheel delta into the daemon's discrete scroll units.
pub fn wheel_to_scroll(value: i32) -> f64 {
    // evdev counts detents, the daemon exchanges 120ths like Windows does.
    f64::from(value) * 120.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn desktop() -> Bounds {
        Bounds::new(0, 0, 1920, 1080)
    }

    #[test]
    fn a_mouse_is_a_pointer_even_with_extra_buttons() {
        assert_eq!(
            classify(
                &[EV_KEY, EV_REL],
                &[0x110, 0x111, 0x113],
                &[REL_X, REL_Y],
                &[]
            ),
            DeviceKind::Pointer
        );
    }

    #[test]
    fn a_keyboard_is_recognised_by_its_letter_keys() {
        let codes: Vec<u16> = (1..=58).collect();
        assert_eq!(classify(&[EV_KEY], &codes, &[], &[]), DeviceKind::Keyboard);
    }

    #[test]
    fn a_button_only_device_is_not_mistaken_for_a_keyboard() {
        // Lid switches and power buttons must not be grabbed as keyboards.
        assert_eq!(classify(&[EV_KEY], &[0x110], &[], &[]), DeviceKind::Ignored);
    }

    #[test]
    fn system_button_arrays_are_never_taken_for_keyboards() {
        // Grabbing these would cost the user their power and brightness keys.
        let power_button = [116u16];
        let video_bus = [224u16, 225, 227];
        let hotkey_array = [431u16, 432, 148];
        for codes in [&power_button[..], &video_bus[..], &hotkey_array[..]] {
            assert_eq!(classify(&[EV_KEY], codes, &[], &[]), DeviceKind::Ignored);
        }
    }

    #[test]
    fn a_typing_keyboard_is_identified_by_its_letters() {
        let mut codes: Vec<u16> = (1..=58).collect();
        assert!(codes.contains(&KEY_A) && codes.contains(&KEY_Z) && codes.contains(&KEY_SPACE));
        assert_eq!(classify(&[EV_KEY], &codes, &[], &[]), DeviceKind::Keyboard);
        codes.retain(|code| *code != KEY_SPACE);
        assert_eq!(
            classify(&[EV_KEY], &codes, &[], &[]),
            DeviceKind::Ignored,
            "a partial key set is not a typing keyboard"
        );
    }

    #[test]
    fn a_touchpad_is_kept_apart_from_a_mouse() {
        assert_eq!(
            classify(&[EV_KEY, EV_ABS], &[0x110], &[], &[0, 1]),
            DeviceKind::AbsolutePointer
        );
    }

    #[test]
    fn only_real_pointer_buttons_are_forwarded() {
        assert_eq!(button_code(0x110), Some(0x110));
        assert_eq!(button_code(0x117), Some(0x117));
        assert_eq!(button_code(30), None, "a letter key is not a button");
        assert_eq!(button_code(0x160), None, "BTN_TOOL codes are not buttons");
    }

    #[test]
    fn the_estimate_is_confined_to_the_desktop() {
        let mut tracker = EdgeTracker::new(desktop());
        tracker.motion(100_000, 100_000, &[]);
        assert_eq!(tracker.position(), (1919, 1079));
        tracker.motion(-100_000, -100_000, &[]);
        assert_eq!(tracker.position(), (0, 0));
    }

    #[test]
    fn crossing_needs_sustained_outward_push_against_the_edge() {
        let mut tracker = EdgeTracker::new(desktop());
        // Travelling to the edge is not itself a crossing: the whole delta was
        // spent crossing the screen, none of it clipped.
        assert_eq!(tracker.motion(959, 0, &[Edge::Right]), None);
        assert_eq!(tracker.position(), (1919, 540));
        // A nudge past it below the threshold still is not.
        assert_eq!(tracker.motion(4, 0, &[Edge::Right]), None);
        // Sustained pressure is.
        assert_eq!(
            tracker.motion(EDGE_PUSH_THRESHOLD, 0, &[Edge::Right]),
            Some(Crossing { edge: Edge::Right })
        );
    }

    #[test]
    fn a_hard_flick_into_the_edge_crosses_immediately() {
        // Slamming the mouse at the edge is the usual gesture; the clipped
        // remainder is large, so it should not need a second shove.
        let mut tracker = EdgeTracker::new(desktop());
        assert_eq!(
            tracker.motion(5000, 0, &[Edge::Right]),
            Some(Crossing { edge: Edge::Right })
        );
    }

    #[test]
    fn an_edge_without_a_machine_behind_it_never_captures() {
        let mut tracker = EdgeTracker::new(desktop());
        tracker.motion(5000, 0, &[Edge::Left]);
        assert_eq!(tracker.motion(5000, 0, &[Edge::Left]), None);
    }

    #[test]
    fn a_crossing_fires_once_until_the_pointer_pulls_back() {
        let mut tracker = EdgeTracker::new(desktop());
        assert!(tracker.motion(5000, 0, &[Edge::Right]).is_some());
        // Still shoving at the same edge: no repeat.
        assert_eq!(tracker.motion(5000, 0, &[Edge::Right]), None);
        // Pull away and push again: a new crossing.
        tracker.motion(-500, 0, &[Edge::Right]);
        assert!(tracker.motion(5000, 0, &[Edge::Right]).is_some());
    }

    #[test]
    fn pulling_away_from_an_edge_clears_its_pressure() {
        let mut tracker = EdgeTracker::new(desktop());
        tracker.motion(959, 0, &[Edge::Right]);
        tracker.motion(6, 0, &[Edge::Right]);
        tracker.motion(-1, 0, &[Edge::Right]);
        // The earlier partial push must not count towards the next one.
        assert_eq!(tracker.motion(7, 0, &[Edge::Right]), None);
    }

    #[test]
    fn vertical_edges_are_tracked_independently() {
        let mut tracker = EdgeTracker::new(desktop());
        assert_eq!(tracker.motion(0, -540, &[Edge::Top]), None);
        assert_eq!(
            tracker.motion(0, -EDGE_PUSH_THRESHOLD, &[Edge::Top]),
            Some(Crossing { edge: Edge::Top })
        );
    }

    #[test]
    fn a_layout_change_keeps_the_estimate_inside_the_new_desktop() {
        let mut tracker = EdgeTracker::new(desktop());
        tracker.motion(5000, 5000, &[]);
        tracker.set_bounds(Bounds::new(0, 0, 1280, 720));
        assert_eq!(tracker.position(), (1279, 719));
    }

    #[test]
    fn a_zero_sized_desktop_is_refused_rather_than_dividing_by_zero() {
        let bounds = Bounds::new(0, 0, 0, 0);
        assert_eq!((bounds.width, bounds.height), (1, 1));
    }

    #[test]
    fn a_kernel_bitmask_decodes_to_its_set_codes() {
        // bit 0 and bit 1 in byte 0, bit 0 in byte 1 is code 8.
        assert_eq!(decode_bits(&[0b0000_0011, 0b0000_0001]), vec![0, 1, 8]);
        assert!(decode_bits(&[0, 0]).is_empty());
    }

    #[test]
    fn an_input_event_record_is_parsed_from_its_kernel_layout() {
        let mut record = [0u8; EVENT_SIZE];
        record[16..18].copy_from_slice(&EV_REL.to_ne_bytes());
        record[18..20].copy_from_slice(&REL_X.to_ne_bytes());
        record[20..24].copy_from_slice(&(-9i32).to_ne_bytes());
        assert_eq!(parse_event(&record), Some((EV_REL, REL_X, -9)));
    }

    #[test]
    fn a_truncated_record_is_discarded_rather_than_misread() {
        assert_eq!(parse_event(&[0u8; 8]), None);
    }

    #[test]
    fn discovery_rejects_a_directory_without_input_devices() {
        let empty = std::env::temp_dir().join("mwb-evdev-empty-probe");
        std::fs::create_dir_all(&empty).unwrap();
        assert!(discover(&empty).is_err());
        std::fs::remove_dir_all(&empty).ok();
    }

    #[test]
    fn real_devices_are_discovered_and_classified_when_readable() {
        let path = Path::new("/dev/input");
        if !path.exists() {
            return;
        }
        // A build machine without readable input devices is fine.
        if let Ok(devices) = discover(path) {
            assert!(!devices.is_empty());
            for device in &devices {
                assert!(
                    matches!(
                        device.kind,
                        DeviceKind::Keyboard | DeviceKind::Pointer | DeviceKind::AbsolutePointer
                    ),
                    "{} was classified {:?}",
                    device.name,
                    device.kind
                );
                assert!(!device.is_grabbed(), "discovery must not grab anything");
                assert!(
                    !device.name.starts_with("Mouse Without Borders virtual"),
                    "our own injected devices must never be captured"
                );
            }
        }
    }

    #[test]
    fn reading_an_idle_device_returns_immediately() {
        // A blocking read here would hang any caller that polls devices in a
        // loop, and would hold a grab open while it waited.
        let path = Path::new("/dev/input");
        if !path.exists() {
            return;
        }
        let Ok(mut devices) = discover(path) else {
            return;
        };
        let started = std::time::Instant::now();
        for device in devices.iter_mut() {
            let _ = device.read_events();
        }
        assert!(
            started.elapsed() < std::time::Duration::from_millis(500),
            "reading idle devices blocked for {:?}",
            started.elapsed()
        );
    }

    #[test]
    fn wheel_detents_become_windows_scroll_units() {
        assert_eq!(wheel_to_scroll(1), 120.0);
        assert_eq!(wheel_to_scroll(-2), -240.0);
    }
}
