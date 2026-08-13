// Copyright (c) Microsoft Corporation
// The Microsoft Corporation licenses this file to you under the MIT license.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Edge {
    Left,
    Right,
    Top,
    Bottom,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum KeyState {
    Pressed,
    Released,
}

/// Which mechanism delivers injected input to the desktop.
///
/// The portal is the sandbox-friendly default. `Uinput` creates kernel input
/// devices instead, which the compositor cannot revoke and which therefore
/// keep working while the session is locked.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InjectBackend {
    #[default]
    Portal,
    Uinput,
    /// Prefer uinput when the device is usable, else fall back to the portal.
    Auto,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ButtonState {
    Pressed,
    Released,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CaptureTarget {
    pub edge: Edge,
    /// Restricts this target's barrier to one compositor zone. Omitted means
    /// every exterior monitor on the selected edge.
    #[serde(default)]
    pub zone: Option<[i32; 4]>,
    /// Opaque identifier echoed when this target's barrier activates.
    pub target: String,
}

#[derive(Debug, Deserialize, PartialEq)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum Command {
    Ping {
        #[serde(default)]
        id: Option<Value>,
    },
    CaptureInit {
        #[serde(default)]
        id: Option<Value>,
        /// Legacy single-target edge. It remains accepted when `targets` is
        /// omitted and may be omitted by multi-target clients.
        #[serde(default)]
        edge: Option<Edge>,
        #[serde(default)]
        restore_token: Option<String>,
        /// Restricts the pointer barrier to one monitor, as `[x, y, width,
        /// height]` in the compositor's logical pixels. Omitted means every
        /// monitor on that side of the desktop hands the pointer over.
        #[serde(default)]
        zone: Option<[i32; 4]>,
        /// Multi-target barrier definitions. When present these replace the
        /// legacy top-level `edge` and `zone` fields.
        #[serde(default)]
        targets: Option<Vec<CaptureTarget>>,
    },
    CaptureRelease {
        #[serde(default)]
        id: Option<Value>,
        #[serde(default)]
        cursor_position: Option<[f64; 2]>,
    },
    CaptureEnable {
        #[serde(default)]
        id: Option<Value>,
    },
    CaptureDisable {
        #[serde(default)]
        id: Option<Value>,
    },
    CaptureStop {
        #[serde(default)]
        id: Option<Value>,
    },
    InjectInit {
        #[serde(default)]
        id: Option<Value>,
        #[serde(default)]
        restore_token: Option<String>,
        /// Which injection path to use: "portal", "uinput" or "auto".
        #[serde(default)]
        backend: Option<InjectBackend>,
        /// Desktop geometry for the uinput absolute pointer axes.
        #[serde(default)]
        screen: Option<[i32; 4]>,
    },
    InjectKey {
        #[serde(default)]
        id: Option<Value>,
        keycode: u32,
        state: KeyState,
    },
    InjectPointerMotion {
        #[serde(default)]
        id: Option<Value>,
        dx: f32,
        dy: f32,
    },
    InjectPointerAbsolute {
        #[serde(default)]
        id: Option<Value>,
        x: f32,
        y: f32,
    },
    InjectButton {
        #[serde(default)]
        id: Option<Value>,
        button: u32,
        state: ButtonState,
    },
    InjectScroll {
        #[serde(default)]
        id: Option<Value>,
        dx: f32,
        dy: f32,
        #[serde(default)]
        discrete: bool,
    },
    InjectStop {
        #[serde(default)]
        id: Option<Value>,
    },
    Shutdown {
        #[serde(default)]
        id: Option<Value>,
    },
}

impl Command {
    pub fn id(&self) -> Option<Value> {
        match self {
            Self::Ping { id }
            | Self::CaptureInit { id, .. }
            | Self::CaptureRelease { id, .. }
            | Self::CaptureEnable { id }
            | Self::CaptureDisable { id }
            | Self::CaptureStop { id }
            | Self::InjectInit { id, .. }
            | Self::InjectKey { id, .. }
            | Self::InjectPointerMotion { id, .. }
            | Self::InjectPointerAbsolute { id, .. }
            | Self::InjectButton { id, .. }
            | Self::InjectScroll { id, .. }
            | Self::InjectStop { id }
            | Self::Shutdown { id } => id.clone(),
        }
    }
}

pub fn response_ok(id: Option<Value>, result: Value) -> Value {
    json!({
        "type": "response",
        "id": id,
        "ok": true,
        "result": result,
    })
}

pub fn response_error(id: Option<Value>, error: impl Into<String>) -> Value {
    json!({
        "type": "response",
        "id": id,
        "ok": false,
        "error": error.into(),
    })
}

pub fn event(name: &str, fields: Value) -> Value {
    let mut value = json!({
        "type": "event",
        "event": name,
    });
    if let (Some(target), Some(source)) = (value.as_object_mut(), fields.as_object()) {
        target.extend(source.clone());
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_capture_init() {
        let command: Command = serde_json::from_str(
            r#"{"command":"capture_init","id":7,"edge":"right","restore_token":"token"}"#,
        )
        .unwrap();

        assert_eq!(
            command,
            Command::CaptureInit {
                id: Some(json!(7)),
                edge: Some(Edge::Right),
                restore_token: Some("token".into()),
                zone: None,
                targets: None,
            }
        );
    }

    #[test]
    fn parses_capture_init_with_a_target_monitor() {
        let command: Command = serde_json::from_str(
            r#"{"command":"capture_init","edge":"top","zone":[1920,0,2560,1440]}"#,
        )
        .unwrap();

        assert_eq!(
            command,
            Command::CaptureInit {
                id: None,
                edge: Some(Edge::Top),
                restore_token: None,
                zone: Some([1920, 0, 2560, 1440]),
                targets: None,
            }
        );
    }

    #[test]
    fn parses_multi_target_capture_without_legacy_edge() {
        let command: Command = serde_json::from_str(
            r#"{"command":"capture_init","targets":[{"edge":"left","target":"alpha"},{"edge":"bottom","zone":[0,0,1920,1080],"target":"beta"}]}"#,
        )
        .unwrap();

        assert_eq!(
            command,
            Command::CaptureInit {
                id: None,
                edge: None,
                restore_token: None,
                zone: None,
                targets: Some(vec![
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
            }
        );
    }

    #[test]
    fn omits_optional_command_fields() {
        let command: Command =
            serde_json::from_str(r#"{"command":"inject_scroll","dx":0.0,"dy":120.0}"#).unwrap();

        assert_eq!(
            command,
            Command::InjectScroll {
                id: None,
                dx: 0.0,
                dy: 120.0,
                discrete: false,
            }
        );
    }

    #[test]
    fn parses_capture_enable_and_disable() {
        assert_eq!(
            serde_json::from_str::<Command>(r#"{"command":"capture_enable","id":"on"}"#).unwrap(),
            Command::CaptureEnable {
                id: Some(json!("on")),
            }
        );
        assert_eq!(
            serde_json::from_str::<Command>(r#"{"command":"capture_disable","id":"off"}"#).unwrap(),
            Command::CaptureDisable {
                id: Some(json!("off")),
            }
        );
    }

    #[test]
    fn produces_stable_response_shape() {
        assert_eq!(
            response_ok(Some(json!("request-1")), json!({ "pong": true })),
            json!({
                "type": "response",
                "id": "request-1",
                "ok": true,
                "result": { "pong": true },
            })
        );
    }
}
