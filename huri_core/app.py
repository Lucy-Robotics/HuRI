import logging
import os

from ray.serve import Application

from huri_core.core.huri import HuRI
from huri_core.core.plugins import load_events, load_modules, parse_selection
from huri_core.modules.factory import bind_deployment_handles

logger = logging.getLogger("ray.serve")


def build_app() -> Application:
    # HURI_MODULES="stt,rag,tts" restricts which modules this server registers —
    # and therefore which Serve deployments bind_deployment_handles() builds, so
    # a host that cannot run e.g. CosyVoice never creates the TTS replica. It may
    # also list import paths (my_pkg.servo:Servo) of unpackaged modules.
    # Default-deny: unset or empty registers no module at all, because any
    # installed package can provide one. scripts/install_local.sh and the
    # shipped serve configs set it.
    modules = load_modules(parse_selection(os.environ.get("HURI_MODULES")))
    if modules:
        logger.info("[HuRI] enabled modules (HURI_MODULES): %s", sorted(modules))
    else:
        logger.warning(
            "[HuRI] HURI_MODULES is unset or empty: no module is enabled and every "
            "session will be rejected. Set it, e.g. HURI_MODULES=vad,stt,tag,qag,rag"
        )
    events = load_events()
    handles = bind_deployment_handles(modules)
    app: Application = HuRI.bind(modules, handles, events)  # type: ignore[attr-defined]
    return app


app = build_app()
