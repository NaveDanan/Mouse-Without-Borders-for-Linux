// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

//! Kernel-level input injection through `/dev/uinput`.
//!
//! The RemoteDesktop portal cannot reach a locked session: the compositor
//! destroys every injected device the moment the screen locks, and it does so
//! for its own private API too, so no portal-based client can type a password.
//! A uinput device is indistinguishable from physical hardware at the evdev
//! layer, so the lock screen accepts it exactly like a real keyboard.
//!
//! Three devices are created rather than one. libinput classifies a device by
//! the axes it advertises, and mixing relative and absolute pointer axes on a
//! single node makes that classification ambiguous.

use std::ffi::c_int;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::fd::{AsRawFd, RawFd};
use std::path::Path;

use anyhow::{Context as _, Result, anyhow, bail};
use rustix::time::{ClockId, clock_gettime};
use serde_json::{Value, json};

use crate::protocol::{ButtonState, KeyState};

pub const UINPUT_PATH: &str = "/dev/uinput";

const UI_DEV_CREATE: u64 = 0x5501;
const UI_DEV_DESTROY: u64 = 0x5502;
const UI_SET_EVBIT: u64 = 0x4004_5564;
const UI_SET_KEYBIT: u64 = 0x4004_5565;
const UI_SET_RELBIT: u64 = 0x4004_5566;
const UI_SET_ABSBIT: u64 = 0x4004_5567;

const EV_SYN: u16 = 0x00;
const EV_KEY: u16 = 0x01;
const EV_REL: u16 = 0x02;
const EV_ABS: u16 = 0x03;

const SYN_REPORT: u16 = 0;
const REL_X: u16 = 0x00;
const REL_Y: u16 = 0x01;
const REL_HWHEEL: u16 = 0x06;
const REL_WHEEL: u16 = 0x08;
const ABS_X: u16 = 0x00;
const ABS_Y: u16 = 0x01;

/// Highest evdev key code the virtual keyboard advertises. This covers the
/// whole standard keyboard range that [`crate`]'s Windows mapping produces.
const KEY_CODE_MAX: u16 = 248;
/// Pointer buttons, matching the codes the daemon already sends.
const BUTTON_CODES: [u16; 5] = [0x110, 0x111, 0x112, 0x113, 0x114];

/// Wheel clicks are reported to evdev in whole detents.
const SCROLL_UNITS_PER_DETENT: f32 = 120.0;

fn ioctl(fd: RawFd, request: u64, value: c_int) -> Result<()> {
    // SAFETY: `fd` is an open uinput descriptor owned by the caller and the
    // request codes are the fixed uinput constants declared above.
    let result = unsafe { libc::ioctl(fd, request as libc::c_ulong, value) };
    if result < 0 {
        return Err(std::io::Error::last_os_error()).context("uinput ioctl failed");
    }
    Ok(())
}

/// The `uinput_user_dev` setup structure written before `UI_DEV_CREATE`.
fn device_payload(name: &str, abs_max: Option<(i32, i32)>) -> Vec<u8> {
    let mut payload = Vec::with_capacity(1116);
    let mut fixed_name = [0u8; 80];
    let bytes = name.as_bytes();
    let length = bytes.len().min(79);
    fixed_name[..length].copy_from_slice(&bytes[..length]);
    payload.extend_from_slice(&fixed_name);
    payload.extend_from_slice(&3u16.to_ne_bytes()); // bustype: BUS_USB
    payload.extend_from_slice(&0x1d6bu16.to_ne_bytes()); // vendor
    payload.extend_from_slice(&0x0104u16.to_ne_bytes()); // product
    payload.extend_from_slice(&1u16.to_ne_bytes()); // version
    payload.extend_from_slice(&0i32.to_ne_bytes()); // ff_effects_max

    let mut absmax = [0i32; 64];
    if let Some((width, height)) = abs_max {
        absmax[ABS_X as usize] = width;
        absmax[ABS_Y as usize] = height;
    }
    for value in absmax {
        payload.extend_from_slice(&value.to_ne_bytes());
    }
    for _ in 0..(64 * 3) {
        // absmin, absfuzz and absflat are all zero for a plain pointer.
        payload.extend_from_slice(&0i32.to_ne_bytes());
    }
    payload
}

/// One created virtual device node.
struct VirtualDevice {
    file: File,
}

impl VirtualDevice {
    fn new(
        name: &str,
        events: &[u16],
        keys: &[u16],
        rel_axes: &[u16],
        abs_axes: &[u16],
        abs_max: Option<(i32, i32)>,
    ) -> Result<Self> {
        let file = OpenOptions::new()
            .write(true)
            .open(UINPUT_PATH)
            .with_context(|| format!("cannot open {UINPUT_PATH} for {name}"))?;
        let fd = file.as_raw_fd();
        for event in events {
            ioctl(fd, UI_SET_EVBIT, c_int::from(*event as i16))?;
        }
        for key in keys {
            ioctl(fd, UI_SET_KEYBIT, c_int::from(*key as i32 as i16))?;
        }
        for axis in rel_axes {
            ioctl(fd, UI_SET_RELBIT, c_int::from(*axis as i16))?;
        }
        for axis in abs_axes {
            ioctl(fd, UI_SET_ABSBIT, c_int::from(*axis as i16))?;
        }
        let mut device = Self { file };
        device
            .file
            .write_all(&device_payload(name, abs_max))
            .with_context(|| format!("cannot describe the virtual device {name}"))?;
        ioctl(fd, UI_DEV_CREATE, 0).with_context(|| format!("cannot create {name}"))?;
        Ok(device)
    }

    fn emit(&mut self, event_type: u16, code: u16, value: i32) -> Result<()> {
        let time = clock_gettime(ClockId::Monotonic);
        let mut buffer = Vec::with_capacity(24);
        buffer.extend_from_slice(&(time.tv_sec as i64).to_ne_bytes());
        buffer.extend_from_slice(&(time.tv_nsec as i64 / 1000).to_ne_bytes());
        buffer.extend_from_slice(&event_type.to_ne_bytes());
        buffer.extend_from_slice(&code.to_ne_bytes());
        buffer.extend_from_slice(&value.to_ne_bytes());
        self.file
            .write_all(&buffer)
            .context("cannot write a uinput event")?;
        Ok(())
    }

    fn sync(&mut self) -> Result<()> {
        self.emit(EV_SYN, SYN_REPORT, 0)
    }
}

impl Drop for VirtualDevice {
    fn drop(&mut self) {
        // Best effort: the descriptor closing also removes the device.
        let _ = ioctl(self.file.as_raw_fd(), UI_DEV_DESTROY, 0);
    }
}

/// Report whether uinput injection can be used by this process.
pub fn is_available() -> bool {
    Path::new(UINPUT_PATH).exists() && OpenOptions::new().write(true).open(UINPUT_PATH).is_ok()
}

/// Explain, for a status message, why uinput is unusable.
pub fn availability_error() -> Option<String> {
    match OpenOptions::new().write(true).open(UINPUT_PATH) {
        Ok(_) => None,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Some(format!("{UINPUT_PATH} is missing; load the uinput kernel module"))
        }
        Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => Some(format!(
            "no write access to {UINPUT_PATH}; add this user to the group owning it"
        )),
        Err(error) => Some(format!("{UINPUT_PATH} is unusable: {error}")),
    }
}

/// A keyboard and pointer pair that the compositor treats as real hardware.
pub struct UinputInjector {
    keyboard: VirtualDevice,
    pointer: VirtualDevice,
    absolute: VirtualDevice,
    width: i32,
    height: i32,
    origin_x: i32,
    origin_y: i32,
}

impl UinputInjector {
    /// Create the virtual devices for a desktop spanning `screen`.
    ///
    /// `screen` is the x, y, width and height the daemon reports for the
    /// combined desktop; the absolute axes are ranged over it so the existing
    /// absolute coordinates map across without rescaling.
    pub fn new(screen: [i32; 4]) -> Result<Self> {
        let [origin_x, origin_y, width, height] = screen;
        if width <= 0 || height <= 0 {
            bail!("the desktop size must be positive to place an absolute pointer");
        }
        if let Some(reason) = availability_error() {
            bail!(reason);
        }

        let keys: Vec<u16> = (1..=KEY_CODE_MAX).collect();
        let keyboard = VirtualDevice::new(
            "Mouse Without Borders virtual keyboard",
            &[EV_KEY, EV_SYN],
            &keys,
            &[],
            &[],
            None,
        )?;
        let pointer = VirtualDevice::new(
            "Mouse Without Borders virtual pointer",
            &[EV_KEY, EV_REL, EV_SYN],
            &BUTTON_CODES,
            &[REL_X, REL_Y, REL_WHEEL, REL_HWHEEL],
            &[],
            None,
        )?;
        let absolute = VirtualDevice::new(
            "Mouse Without Borders virtual absolute pointer",
            &[EV_KEY, EV_ABS, EV_REL, EV_SYN],
            &BUTTON_CODES,
            &[REL_WHEEL, REL_HWHEEL],
            &[ABS_X, ABS_Y],
            Some((width - 1, height - 1)),
        )?;

        Ok(Self {
            keyboard,
            pointer,
            absolute,
            width,
            height,
            origin_x,
            origin_y,
        })
    }

    /// Describe the devices for the daemon, mirroring the portal's report.
    pub fn describe(&self) -> Value {
        json!({
            "backend": "uinput",
            "capabilities": {
                "keyboard": true,
                "pointer": true,
                "pointer_absolute": true,
                "button": true,
                "scroll": true,
            },
            "regions": [{
                "x": self.origin_x,
                "y": self.origin_y,
                "width": self.width,
                "height": self.height,
                "scale": 1.0,
                "mapping_id": Value::Null,
            }],
        })
    }

    pub fn key(&mut self, keycode: u32, state: KeyState) -> Result<()> {
        let code = u16::try_from(keycode)
            .map_err(|_| anyhow!("key code {keycode} is outside the evdev range"))?;
        if code > KEY_CODE_MAX {
            bail!("key code {code} is outside the advertised keyboard range");
        }
        let value = match state {
            KeyState::Pressed => 1,
            KeyState::Released => 0,
        };
        self.keyboard.emit(EV_KEY, code, value)?;
        self.keyboard.sync()
    }

    pub fn button(&mut self, button: u32, state: ButtonState) -> Result<()> {
        let code = u16::try_from(button)
            .map_err(|_| anyhow!("button code {button} is outside the evdev range"))?;
        if !BUTTON_CODES.contains(&code) {
            bail!("button code {code} is not one of the advertised pointer buttons");
        }
        let value = match state {
            ButtonState::Pressed => 1,
            ButtonState::Released => 0,
        };
        self.pointer.emit(EV_KEY, code, value)?;
        self.pointer.sync()
    }

    pub fn motion(&mut self, dx: f32, dy: f32) -> Result<()> {
        ensure_finite(&[dx, dy])?;
        // A zero delta is how the daemon nudges the compositor awake; the sync
        // alone is enough for that and avoids a spurious pointer jump.
        if dx != 0.0 {
            self.pointer.emit(EV_REL, REL_X, dx.round() as i32)?;
        }
        if dy != 0.0 {
            self.pointer.emit(EV_REL, REL_Y, dy.round() as i32)?;
        }
        self.pointer.sync()
    }

    pub fn absolute(&mut self, x: f32, y: f32) -> Result<()> {
        ensure_finite(&[x, y])?;
        let local_x = (x.round() as i32 - self.origin_x).clamp(0, self.width - 1);
        let local_y = (y.round() as i32 - self.origin_y).clamp(0, self.height - 1);
        self.absolute.emit(EV_ABS, ABS_X, local_x)?;
        self.absolute.emit(EV_ABS, ABS_Y, local_y)?;
        self.absolute.sync()
    }

    pub fn scroll(&mut self, dx: f32, dy: f32, discrete: bool) -> Result<()> {
        ensure_finite(&[dx, dy])?;
        let (horizontal, vertical) = if discrete {
            (
                (dx / SCROLL_UNITS_PER_DETENT).round() as i32,
                (dy / SCROLL_UNITS_PER_DETENT).round() as i32,
            )
        } else {
            (dx.round() as i32, dy.round() as i32)
        };
        if horizontal == 0 && vertical == 0 {
            return Ok(());
        }
        if horizontal != 0 {
            self.pointer.emit(EV_REL, REL_HWHEEL, horizontal)?;
        }
        if vertical != 0 {
            // evdev counts a wheel click up as positive, the daemon sends the
            // Windows convention where scrolling down is positive.
            self.pointer.emit(EV_REL, REL_WHEEL, -vertical)?;
        }
        self.pointer.sync()
    }
}

fn ensure_finite(values: &[f32]) -> Result<()> {
    if values.iter().any(|value| !value.is_finite()) {
        bail!("input injection values must be finite");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn device_payload_has_the_kernel_expected_size() {
        // 80 name + 8 input_id + 4 ff_effects_max + 4 * 64 * 4 abs arrays.
        assert_eq!(device_payload("probe", None).len(), 1116);
    }

    #[test]
    fn absolute_range_is_written_into_the_payload() {
        let payload = device_payload("probe", Some((1919, 1079)));
        let base = 80 + 8 + 4;
        let read = |axis: usize| {
            let start = base + axis * 4;
            i32::from_ne_bytes(payload[start..start + 4].try_into().unwrap())
        };
        assert_eq!(read(ABS_X as usize), 1919);
        assert_eq!(read(ABS_Y as usize), 1079);
    }

    #[test]
    fn a_long_device_name_is_truncated_rather_than_overflowing() {
        let payload = device_payload(&"n".repeat(200), None);
        assert_eq!(payload.len(), 1116);
        assert_eq!(payload[79], 0, "the name must stay NUL terminated");
    }

    #[test]
    fn non_finite_injection_values_are_rejected() {
        assert!(ensure_finite(&[f32::NAN]).is_err());
        assert!(ensure_finite(&[f32::INFINITY, 0.0]).is_err());
        assert!(ensure_finite(&[0.0, 1.5]).is_ok());
    }

    #[test]
    fn the_kernel_registers_all_three_devices() {
        // Skips where uinput is not writable, which is the normal state on a
        // build machine; on a real desktop it proves the payload and ioctl
        // sequence are accepted by the kernel.
        if !is_available() {
            eprintln!("skipping uinput device test: {:?}", availability_error());
            return;
        }
        let injector = UinputInjector::new([0, 0, 1920, 1080])
            .expect("the virtual devices should be created");
        let listed = std::fs::read_to_string("/proc/bus/input/devices")
            .expect("the kernel device list should be readable");
        for name in [
            "Mouse Without Borders virtual keyboard",
            "Mouse Without Borders virtual pointer",
            "Mouse Without Borders virtual absolute pointer",
        ] {
            assert!(listed.contains(name), "{name} is missing from the kernel");
        }
        drop(injector);
    }

    #[test]
    fn a_rejected_desktop_size_does_not_create_devices() {
        assert!(UinputInjector::new([0, 0, 0, 1080]).is_err());
    }

    #[test]
    fn availability_reports_a_reason_when_uinput_cannot_be_opened() {
        // Whatever this machine allows, the two answers must stay consistent.
        assert_eq!(is_available(), availability_error().is_none());
    }
}
