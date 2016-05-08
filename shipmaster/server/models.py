import os
import re
import time
import shutil
import subprocess
from random import randint
from collections import OrderedDict

from channels.channel import Channel
from django.utils.functional import cached_property

from docker import Client
from compose.cli.main import filter_containers_to_service_names
from compose.container import Container
from compose.cli.log_printer import LogPrinter, build_log_presenters
from ruamel import yaml

from shipmaster.base.builder import Project
from shipmaster.base.config import ProjectConf
from . import services


class YamlPath:
    @property
    def yaml(self):
        raise NotImplementedError


class YamlModel:

    @classmethod
    def parent_class(cls, path):
        raise NotImplementedError

    def __init__(self, **kwargs):
        self.path = YamlPath()
        self.dict = OrderedDict(**kwargs)

    @classmethod
    def load(cls, parent, name):
        model = cls(parent, name)
        with open(model.path.yaml, 'r') as file:
            model.dict = yaml.load(file)
        return model

    @classmethod
    def from_path(cls, path):
        parent_path = os.path.dirname(os.path.dirname(path))
        parent = cls.parent_class.from_path(parent_path)
        return cls.load(parent, os.path.basename(path))

    def save(self):
        with open(self.path.yaml, 'w') as file:
            file.write(yaml.dump(self.dict))


class ShipmasterPath:

    def __init__(self, data_path):
        self.data_path = data_path

    @property
    def repos_dir(self):
        return os.path.join(self.data_path, 'repos')

    @property
    def ssh_dir(self):
        return os.path.join(self.data_path, 'ssh')

    @property
    def keys_dir(self):
        return os.path.join(self.ssh_dir, 'keys')

    @property
    def ssh_config(self):
        return os.path.join(self.ssh_dir, 'ssh_config')


class Shipmaster:

    def __init__(self, data_path):
        self.path = ShipmasterPath(data_path)

    @classmethod
    def from_path(cls, path):
        return cls(path)

    def update_ssh_config(self):
        with open(self.path.ssh_config, 'w') as config:
            for repo in self.repositories:
                config.write('Host '+repo.git_project_host+'\n')
                config.write('  HostName '+repo.git_host+'\n')
                config.write('  User git\n')
                config.write('  IdentityFile '+repo.path.private_key+'\n')

    @property
    def repositories(self):
        for repo_name in os.listdir(self.path.repos_dir):
            yield Repository.load(self, repo_name)


class RepositoryPath(YamlPath):

    def __init__(self, shipmaster, name):
        self.shipmaster = shipmaster
        self.name = name

    @property
    def absolute(self):
        return os.path.join(self.shipmaster.path.repos_dir, self.name)

    @property
    def yaml(self):
        return os.path.join(self.absolute, 'config.yaml')

    @property
    def last_build_number(self):
        return os.path.join(self.absolute, 'last_build_number')

    @property
    def builds(self):
        return os.path.join(self.absolute, 'builds')

    @property
    def public_key(self):
        return self.private_key+'.pub'

    @property
    def private_key(self):
        return os.path.join(self.shipmaster.path.keys_dir, self.name)


class Repository(YamlModel):

    parent_class = Shipmaster

    #                 host      account     repo
    GIT_REGEX = "git@([\w\.]+):([\w\.\-]+)/([\w\.\-]+)\.git"

    def __init__(self, shipmaster, name, **kwargs):
        super().__init__(**kwargs)
        self.shipmaster = shipmaster  # type: Shipmaster
        self.name = name
        self.path = RepositoryPath(shipmaster, name)
        self.git_host = self.git_account = self.git_repo = ''

    def _parse_git(self):
        m = re.search(Repository.GIT_REGEX, self.git)
        if m:
            self.git_host = m.group(1)
            self.git_account = m.group(2)
            self.git_repo = m.group(3)

    @classmethod
    def load(cls, parent, name):
        self = super().load(parent, name)  # type: Repository
        self._parse_git()
        return self

    def increment_build_number(self):
        return increment_number_file(self.path.last_build_number)

    @property
    def git(self):
        return self.dict['git']

    @git.setter
    def git(self, git):
        self.dict['git'] = git

    @property
    def git_project_host(self):
        return "{}.{}".format(self.name, self.git_host)

    @property
    def project_git(self):
        return "git@{}:{}/{}.git".format(self.git_project_host, self.git_account, self.git_repo)

    @property
    def public_key(self):
        return open(self.path.public_key, 'r').read()

    @classmethod
    def create(cls, shipmaster, name, git):
        repo = cls(shipmaster, name, git=git)
        repo._parse_git()

        if not os.path.exists(shipmaster.path.repos_dir):
            os.mkdir(shipmaster.path.repos_dir)

        os.mkdir(repo.path.absolute)
        os.mkdir(repo.path.builds)
        try:
            keygen = "ssh-keygen -q -b 4096 -t rsa -N '' -f {}".format(repo.path.private_key)
            subprocess.check_output(keygen, shell=True, stderr=subprocess.STDOUT)
            repo.save()
        except:
            shutil.rmtree(repo.path.absolute, ignore_errors=True)
            raise

        shipmaster.update_ssh_config()

        return repo

    def builds(self):
        for build in os.listdir(self.path.builds):
            yield Build.load(self, build)

    def __eq__(self, other):
        assert isinstance(other, Repository)
        return self.name == other.name


class BuildPath(YamlPath):

    def __init__(self, repo, number):
        self.repo = repo
        self.number = number

    @property
    def absolute(self):
        return os.path.join(self.repo.path.builds, self.number)

    @property
    def clone_begin(self):
        return os.path.join(self.absolute, 'clone.begin')

    @property
    def clone_end(self):
        return os.path.join(self.absolute, 'clone.end')

    @property
    def build_begin(self):
        return os.path.join(self.absolute, 'build.begin')

    @property
    def build_end(self):
        return os.path.join(self.absolute, 'build.end')

    @property
    def log(self):
        return os.path.join(self.absolute, 'build.log')

    @property
    def yaml(self):
        return os.path.join(self.absolute, 'build.yaml')

    @property
    def workspace(self):
        return os.path.join(self.absolute, 'workspace')

    @property
    def conf(self):
        return os.path.join(self.workspace, '.shipmaster.yaml')

    @property
    def jobs(self):
        return os.path.join(self.absolute, 'jobs')

    @property
    def last_job_number(self):
        return os.path.join(self.absolute, 'last_job_number')


class Build(YamlModel):

    parent_class = Repository

    def __init__(self, repo, number, **kwargs):
        super().__init__(**kwargs)
        self.repo = repo  # type: Repository
        self.shipmaster = repo.shipmaster  # type: Shipmaster
        self.number = number
        self.path = BuildPath(repo, number)

    @classmethod
    def create(cls, repo, branch):
        build = cls(repo, repo.increment_build_number(), branch=branch)
        os.mkdir(build.path.absolute)
        os.mkdir(build.path.jobs)
        build.save()
        return build

    def increment_job_number(self):
        return increment_number_file(self.path.last_job_number)

    @property
    def branch(self):
        return self.dict['branch']

    @branch.setter
    def branch(self, branch):
        self.dict['branch'] = branch

    def get_project(self, log):
        return Project(
            ProjectConf.from_workspace(self.path.workspace),
            build_num=self.number,
            log=log
        )

    def jobs(self):
        for job in os.listdir(self.path.jobs):
            yield Job.load(self, job)

    def build(self):
        Channel("build-app").send({'path': self.path.absolute})

    def deploy(self):
        Channel("deploy-app").send({'path': self.path.absolute})

    # Timers & Progress

    @property
    def has_cloning_started(self):
        return os.path.exists(self.path.clone_begin)

    @property
    def has_cloning_finished(self):
        return os.path.exists(self.path.clone_end)

    def cloning_started(self):
        assert not self.has_cloning_started
        record_time(self.path.clone_begin)

    def cloning_finished(self):
        assert not self.has_cloning_finished
        record_time(self.path.clone_end)

    @property
    def has_build_started(self):
        return os.path.exists(self.path.build_begin)

    @property
    def has_build_finished(self):
        return os.path.exists(self.path.build_end)

    def build_started(self):
        assert not self.has_build_started
        record_time(self.path.build_begin)

    def build_finished(self):
        assert not self.has_build_finished
        record_time(self.path.build_end)


class JobPath(YamlPath):

    def __init__(self, build, number):
        self.build = build
        self.number = number

    @property
    def absolute(self):
        return os.path.join(self.build.path.jobs, self.number)

    @property
    def yaml(self):
        return os.path.join(self.absolute, 'job.yaml')

    @property
    def log(self):
        return os.path.join(self.absolute, 'job.log')


class Job(YamlModel):

    parent_class = Build

    def __init__(self, build, number, **kwargs):
        super().__init__(**kwargs)
        self.build = build  # type: Build
        self.repo = build.repo  # type: Repository
        self.shipmaster = build.repo.shipmaster  # type: Shipmaster
        self.number = str(number)
        self.path = JobPath(build, number)

    @classmethod
    def create(cls, build):
        job = cls(build, build.increment_job_number())
        os.mkdir(job.path.absolute)
        job.save()
        return job

    def build(self):
        job_channel.send({
            'path': build.path.absolute
        })

    def start(self):
        client = Client('unix://var/run/docker.sock')
        shipmaster_yaml = os.path.join(self.path.workspace, '.shipmaster.yaml')
        conf = ShipmasterConf.from_filename('test', shipmaster_yaml)
        conf.services.environment['GIT_SSH_COMMAND'] = "ssh -F {}".format(self.shipmaster.path.ssh_config)
        conf.services.volumes += ['{0}:{0}'.format(self.shipmaster.path.ssh_dir)]
        containers = services.up(conf, client, log=False)
        cdict = {}
        for c in containers:
            cdict[c.service] = {
                'containerId': c.id,
                'imageId': c.image,
                'repo': c.image_config['RepoTags'][0]
            }
        with open(self.path.containers, 'w') as cf:
            yaml.dump(cdict, cf)

    def log(self):
        client = Client('unix://var/run/docker.sock')
        shipmaster_yaml = os.path.join(self.path.workspace, '.shipmaster.yaml')
        conf = ShipmasterConf.from_filename('test', shipmaster_yaml)
        project = services.get_project(conf, client)
        containers = [Container.from_id(client, cid) for cid in self.containers]
        return services.LogPrinter(
            filter_containers_to_service_names(containers, ['app']),
            build_log_presenters(['app'], False),
            project.events(service_names=['app']),
            cascade_stop=True).run()

    def deploy(self):
        client = Client('unix://var/run/docker.sock')
        shipmaster_yaml = os.path.join(self.path.workspace, '.shipmaster.yaml')
        conf = ShipmasterConf.from_filename('app', shipmaster_yaml)
        with open(self.path.containers, 'r') as cf:
            containers = yaml.load(cf)
        conf.services.services['app']['image'] = containers['app']['imageId']
        conf.services.services['app']['ports'] = []  # '{0}:{0}'.format(randint(2000, 8000))]
        services.up(conf, client, log=False)


def record_time(path):
    with open(path, 'w') as stamp:
        stamp.write(str(time.time()))


def get_time_elapsed(start_path, end_path):
    with open(start_path, 'r') as start_file:
        start = float(start_file.read())
    with open(end_path, 'r') as end_file:
        end = float(end_file.read())
    return end - start


def increment_number_file(path):
    number = 1
    if os.path.exists(path):
        with open(path, 'r') as file:
            number = int(file.read().strip())
            number += 1
    number = str(number)
    with open(path, 'w') as file:
        file.write(number)
    return number
