import os
import subprocess
from .models import Build, Job
from ..base.utils import UnbufferedLineIO
from celery import shared_task


def run(command, log=None, cwd=None, env=None):
    env = {**os.environ, **(env or {})}
    log.write("+ {}".format(' '.join(command)))
    subprocess.run(
        command, cwd=cwd, env=env, stderr=subprocess.STDOUT, stdout=log, check=True
    )


@shared_task
def build_app(path):

    build = Build.from_path(path)

    assert not build.has_cloning_started

    with open(build.path.build_log, 'a') as buffered:

        log = UnbufferedLineIO(buffered)

        git_ssh_command = {"GIT_SSH_COMMAND": "ssh -F {}".format(build.shipmaster.path.ssh_config)}

        build.cloning_started()
        run(["git", "clone",
             "--depth=1",
             "--branch={}".format(build.branch),
             build.repo.project_git,
             build.path.workspace],
            log=log, env=git_ssh_command)
        build.cloning_finished()

        # project doesn't exist until after checkout finishes
        project = build.get_project(log)

        build.build_started()
        project.app.build()
        build.build_finished()


@shared_task
def deploy_app(path):

    build = Build.from_path(path)

    assert not build.has_deployment_started

    with open(build.path.deployment_log, 'a') as buffered:

        log = UnbufferedLineIO(buffered)

        project = build.get_project(log)

        build.deployment_started()
        project.app.deploy()
        build.deployment_finished()


@shared_task
def test_app(path):
    job = Job.from_path(path)
    with open(job.path.log, 'a') as buffered:
        log = UnbufferedLineIO(buffered)
        project = job.get_project(log)
        job.job_started()
        project.test.build()
        job.job_finished()

