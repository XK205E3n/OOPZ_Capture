"""QQ bridge: sends the finished digest image to a QQ group through MaiBot's local relay endpoint.

An add-on module, separate from the recorder (`continuous`), the analyzer (`analyzer`, `digest`) and the
Feishu gateway.  The only places that touch it are `controller._analyze_and_deliver` (one call) and the
settings listing; see docs/QQ_BRIDGE.md.
"""
from .deliver import deliver_digest, failure_notice
from .relay import BridgeConfig, BridgeConfigError, request_id_for, send_image

__all__ = ["BridgeConfig", "BridgeConfigError", "deliver_digest", "failure_notice", "request_id_for", "send_image"]
