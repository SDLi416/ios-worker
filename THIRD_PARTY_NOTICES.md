# Third-party components

The original code in this repository is MIT-licensed. Dependencies retain their own licenses.

- **WebDriverAgent** — downloaded separately from [appium/WebDriverAgent](https://github.com/appium/WebDriverAgent), pinned by `scripts/prepare_ui.py` to commit `3d6a4e03cdd673e540289c610f7e189f8ffea434` (16.15.3). Its upstream BSD license is reproduced in [third_party/WebDriverAgent-LICENSE](third_party/WebDriverAgent-LICENSE). No upstream source or compiled runner is bundled here.
- **FastAPI, Uvicorn, Pillow and Python dependencies** — installed using `requirements.lock`; package code is not vendored. Consult their distributed licenses when redistributing a runtime environment.
- **Apple Xcode, SDKs and simulator runtimes** — installed separately under Apple's terms. No Apple binaries, SDKs or runtime images are distributed here.

The optional launcher is compiled locally from this repository's Swift source. Generated client profiles and certificates are deployment-specific data, not part of the public distribution.
