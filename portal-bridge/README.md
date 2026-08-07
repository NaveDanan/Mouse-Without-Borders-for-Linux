# Mouse Without Borders portal bridge

`mwb-portal-bridge` is a small, rootless helper for a Linux Mouse Without
Borders daemon. It uses XDG Desktop Portal for authorization and the EI
protocol for captured and emulated input. It never opens `/dev/input` or
`/dev/uinput`.

The process reads one JSON object per line from stdin and writes one JSON object
per line to stdout. Do not mix logs with stdout; diagnostics and fatal cleanup
errors go to stderr.

## Build

```sh
cd src/modules/MouseWithoutBorders/Linux/portal-bridge
cargo build --locked --release
cargo test --locked
```

The current implementation requires:

- `org.freedesktop.portal.InputCapture` for capture.
- `org.freedesktop.portal.RemoteDesktop` version 2 or newer for EIS injection.
- A compositor portal backend that implements the requested interfaces.

The portal may display a user-consent dialog. An `init` response is not emitted
until the portal and EI handshake have completed. Persisted portal tokens are
single-use; save the replacement `restore_token` returned by every successful
initialization. InputCapture v1 has no persistence token, so its caller should
disable and retain the authorized bridge session while sharing is stopped, then
enable that same session again on relaunch.

## Message envelope

Commands may carry a string, number, or JSON `id`. Responses echo it:

```json
{"command":"ping","id":1}
{"type":"response","id":1,"ok":true,"result":{"pong":true,"version":"0.1.2"}}
```

Command failures use `{"type":"response","ok":false,"error":"..."}`.
Captured input and lifecycle notifications are unsolicited messages with
`{"type":"event","event":"..."}`. A reader must therefore route responses
by `id` rather than assuming the next output line is its response.

## Capture commands and events

Initialize barriers on the exterior edge of the compositor's combined zone
layout:

```json
{"command":"capture_init","id":"capture","edge":"right","restore_token":null}
```

To route several exterior edges in one authorized portal session, replace the
legacy `edge` and `zone` fields with a non-empty `targets` array. `target` is an
opaque string that is returned with the activation event. Each optional `zone`
is `[x,y,width,height]` in compositor logical coordinates:

```json
{"command":"capture_init","id":"capture","targets":[{"edge":"left","target":"desk-left"},{"edge":"bottom","zone":[0,0,1920,1080],"target":"desk-bottom"}],"restore_token":null}
```

Valid edges are `left`, `right`, `top`, and `bottom`. The response includes
zones, target definitions, requested and accepted barrier metadata, rejected
barrier IDs and metadata, capabilities, and a replacement restore token when
supported. Barrier metadata contains its unique `id`, `position`, `edge`, and
`target`. Initialization fails if no targets were supplied or if the compositor
rejects every requested barrier.

When the pointer crosses a barrier, the bridge emits:

```json
{"type":"event","event":"capture_activated","activation_id":12,"barrier_id":1,"edge":"left","target":"desk-left","cursor_position":[1923.0,400.0]}
{"type":"event","event":"key","keycode":30,"state":"pressed","time_us":1234}
{"type":"event","event":"pointer_motion","dx":4.0,"dy":-1.0,"time_us":1235}
{"type":"event","event":"pointer_absolute","x":100.0,"y":200.0,"time_us":1236}
{"type":"event","event":"button","button":272,"state":"pressed","time_us":1237}
{"type":"event","event":"scroll","dx":0.0,"dy":120,"discrete":true,"time_us":1238}
```

Keycodes and button codes are Linux evdev codes. Discrete scroll values use EI
units, where 120 is one wheel click.

Release active capture and place the local cursor just inside the edge that
actually activated:

```json
{"command":"capture_release","id":2}
```

An explicit portal-coordinate cursor position may be supplied:

```json
{"command":"capture_release","id":2,"cursor_position":[1918.0,400.0]}
```

Close the capture session with `{"command":"capture_stop","id":3}`. If zones
change, `capture_zones_changed` is emitted with `requires_reinitialize:true`.
The caller should stop and initialize capture again; changing barriers in-place
is unreliable on GNOME 46.

## Injection commands

Initialize a RemoteDesktop EIS sender:

```json
{"command":"inject_init","id":"inject","restore_token":null}
```

The response reports the device interfaces actually granted by the compositor.
Only commands for available interfaces succeed:

```json
{"command":"inject_key","id":10,"keycode":30,"state":"pressed"}
{"command":"inject_key","id":11,"keycode":30,"state":"released"}
{"command":"inject_pointer_motion","id":12,"dx":4.0,"dy":-2.0}
{"command":"inject_pointer_absolute","id":13,"x":100.0,"y":200.0}
{"command":"inject_button","id":14,"button":272,"state":"pressed"}
{"command":"inject_button","id":15,"button":272,"state":"released"}
{"command":"inject_scroll","id":16,"dx":0.0,"dy":120.0,"discrete":true}
```

Absolute coordinates must be within a region reported by `inject_device_added`.
Close the RemoteDesktop session with `{"command":"inject_stop","id":17}`.

`{"command":"shutdown","id":99}` closes both portal sessions and exits.

## Wayland security constraints

- The compositor decides when capture activates and may filter or revoke input.
- Capture starts only from a configured pointer barrier; the application cannot
  force immediate global capture.
- Portal input does not work on the login screen, lock screen, or another user's
  session.
- Reserved compositor shortcuts may not be delivered.
- A native X11 session needs a separate X11 backend; XWayland is not a substitute
  for these Wayland portal APIs.
