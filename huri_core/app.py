from ray.serve import Application

from huri_core.core.huri import HuRI
from huri_core.modules.events import get_events
from huri_core.modules.factory import bind_deployment_handles
from huri_core.modules.modules import get_modules


def build_app() -> Application:
    modules = get_modules()
    events = get_events()
    handles = bind_deployment_handles(modules)
    app: Application = HuRI.bind(modules, handles, events)  # type: ignore[attr-defined]
    return app


app = build_app()
