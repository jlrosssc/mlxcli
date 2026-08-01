import pathlib

import webview


webview.create_window("oMLX Admin", "http://localhost:8000/admin",
                      width=1100, height=780)
webview.start(private_mode=False,
              storage_path=str(pathlib.Path.home() / ".omlxpanel-data"))
