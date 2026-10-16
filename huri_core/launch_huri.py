import time

import ray

from huri_core.core.huri import serve

from .app import build_app


def main() -> None:
    ray.init()

    app = build_app()
    time.sleep(0.1)
    try:
        serve.run(app, name="HuRI", blocking=True)
    except KeyboardInterrupt:
        return
    except Exception as e:
        ray.logger.error(e)


if __name__ == "__main__":
    main()
