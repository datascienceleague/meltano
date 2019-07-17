import datetime
import logging
import os
import requests
import threading
import time
import webbrowser

from colorama import Fore

from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler, EVENT_TYPE_MODIFIED
from meltano.core.project import Project
from meltano.core.plugin import PluginInstall, PluginType
from meltano.core.config_service import ConfigService
from meltano.core.compiler.project_compiler import ProjectCompiler
from meltano.core.plugin_invoker import invoker_factory
from meltano.core.runner.dbt import DbtRunner
from meltano.core.runner.singer import SingerRunner
from meltano.api.models import db


class CompileEventHandler(PatternMatchingEventHandler):
    def __init__(self, compiler):
        self.compiler = compiler

        super().__init__(ignore_patterns=["*.m5oc"])

    def on_any_event(self, event):
        try:
            self.compiler.compile()
        except Exception as e:
            logging.error(f"Compilation failed: {str(e)}")


class MeltanoBackgroundCompiler:
    def __init__(self, project: Project, compiler: ProjectCompiler = None):
        self.project = project
        self.compiler = compiler or ProjectCompiler(project)
        self.observer = self.setup_observer()

    @property
    def model_dir(self):
        return self.project.root_dir("model")

    def setup_observer(self):
        event_handler = CompileEventHandler(self.compiler)
        observer = Observer()
        observer.schedule(event_handler, str(self.model_dir), recursive=True)

        return observer

    def start(self):
        try:
            self.observer.start()
            logging.info(f"Auto-compiling models in '{self.model_dir}'")
        except OSError:
            # most probably INotify being full
            logging.warn(f"Model auto-compilation is disabled: INotify limit reached.")

    def stop(self):
        self.observer.stop()


class AirflowWorker(threading.Thread):
    def __init__(self, project: Project, airflow: PluginInstall = None):
        super().__init__()

        self.project = project
        self._plugin = airflow or ConfigService(project).find_plugin("airflow")

    def start_all(self):
        invoker = invoker_factory(db.session, self.project, self._plugin)
        self._webserver = invoker.invoke("webserver")
        self._scheduler = invoker.invoke("scheduler")

    def run(self):
        return self.start_all()

    def stop(self):
        self._webserver.terminate()
        self._scheduler.terminate()


class ELTWorker(threading.Thread):
    def __init__(self, project: Project, schedule_payload: dict):
        super().__init__()

        self._complete = False
        self.project = project
        self.extractor = schedule_payload["extractor"]
        self.loader = schedule_payload["loader"]
        self.transform = schedule_payload.get("transform")
        self.schedule_name = schedule_payload.get("name")
        self.job_id = f'job_{self.schedule_name}_{datetime.datetime.now().strftime("%Y%m%d-%H:%M:%S.%f")}'

    def run(self):
        singer_runner = SingerRunner(
            self.project,
            job_id=self.job_id,
            run_dir=os.getenv("SINGER_RUN_DIR", self.project.meltano_dir("run")),
            target_config_dir=self.project.meltano_dir(PluginType.LOADERS, self.loader),
            tap_config_dir=self.project.meltano_dir(PluginType.EXTRACTORS, self.extractor),
        )

        try:
            if self.transform == "run" or self.transform == "skip":
                print("******** RUN OR SKIP", self.transform)
                singer_runner.run(self.extractor, self.loader)
            if self.transform == "run":
                print("******** RUN!!!!", self.transform)
                dbt_runner = DbtRunner(self.project)
                dbt_runner.run(self.extractor, self.loader, models=self.extractor)
        except Exception as err:
            raise Exception("ELT could not complete, an error happened during the process.")

        self.stop()

    def stop(self):
        self._complete = True


class UIAvailableWorker(threading.Thread):
    def __init__(self, url, open_browser=False):
        super().__init__()

        self._terminate = False
        self.url = url
        self.open_browser = open_browser

    def run(self):
        while not self._terminate:
            try:
                response = requests.get(self.url)
                if response.status_code == 200:
                    print(f"{Fore.GREEN}Meltano is available at {self.url}{Fore.RESET}")
                    if self.open_browser:
                        webbrowser.open(self.url)
                    self._terminate = True

            except:
                pass

            time.sleep(2)

    def stop(self):
        self._terminate = True
