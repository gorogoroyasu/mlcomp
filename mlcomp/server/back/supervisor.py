import datetime
import traceback
from typing import List

from sqlalchemy.orm.exc import ObjectDeletedError

from mlcomp.db.core import Session
from mlcomp.db.enums import ComponentType, TaskStatus, TaskType
from mlcomp.db.models import Task, Auxiliary
from mlcomp.db.providers import \
    ComputerProvider, \
    TaskProvider, \
    DockerProvider, \
    AuxiliaryProvider, DagProvider
from mlcomp.utils.io import yaml_dump, yaml_load
from mlcomp.utils.logging import create_logger
from mlcomp.utils.misc import now
from mlcomp.worker.tasks import execute
from mlcomp.utils.schedule import start_schedule
import mlcomp.worker.tasks as celery_tasks


class SupervisorBuilder:
    def __init__(self):
        self.session = Session.create_session(key='SupervisorBuilder')
        self.logger = create_logger(self.session, 'SupervisorBuilder')
        self.provider = None
        self.computer_provider = None
        self.docker_provider = None
        self.auxiliary_provider = None
        self.dag_provider = None
        self.queues = None
        self.not_ran_tasks = None
        self.dep_status = None
        self.computers = None
        self.auxiliary = {}

    def create_base(self):
        self.session.commit()

        self.provider = TaskProvider(self.session)
        self.computer_provider = ComputerProvider(self.session)
        self.docker_provider = DockerProvider(self.session)
        self.auxiliary_provider = AuxiliaryProvider(self.session)
        self.dag_provider = DagProvider(self.session)

        self.queues = [
            f'{d.computer}_{d.name}' for d in self.docker_provider.all()
            if d.last_activity >= now() - datetime.timedelta(seconds=15)
        ]

        self.auxiliary['queues'] = self.queues

    def load_tasks(self):
        not_ran_tasks = self.provider.by_status(TaskStatus.NotRan)
        self.not_ran_tasks = [task for task in not_ran_tasks if not task.debug]
        self.logger.debug(
            f'Found {len(not_ran_tasks)} not ran tasks',
            ComponentType.Supervisor
        )

        self.dep_status = self.provider.dependency_status(self.not_ran_tasks)

        self.auxiliary['not_ran_tasks'] = [
            {
                'id': t.id,
                'name': t.name,
                'dep_status': [
                    TaskStatus(s).name
                    for s in self.dep_status.get(t.id, set())
                ]
            } for t in not_ran_tasks
        ]

    def load_computers(self):
        computers = self.computer_provider.computers()
        for computer in computers.values():
            computer['gpu'] = [0] * computer['gpu']
            computer['ports'] = set()
            computer['cpu_total'] = computer['cpu']
            computer['memory_total'] = computer['memory']
            computer['gpu_total'] = len(computer['gpu'])
            computer['can_process_tasks'] = computer['can_process_tasks']

        for task in self.provider.by_status(
                TaskStatus.Queued, TaskStatus.InProgress
        ):
            if task.computer_assigned is None:
                continue
            assigned = task.computer_assigned
            comp_assigned = computers[assigned]
            comp_assigned['cpu'] -= task.cpu

            if task.gpu_assigned is not None:
                for g in task.gpu_assigned.split(','):
                    comp_assigned['gpu'][int(g)] = task.id
            comp_assigned['memory'] -= task.memory * 1024

            info = yaml_load(task.additional_info)
            if 'distr_info' in info:
                dist_info = info['distr_info']
                if dist_info['rank'] == 0:
                    comp_assigned['ports'].add(dist_info['master_port'])

        self.computers = [
            {
                **value, 'name': name
            } for name, value in computers.items()
        ]

        self.auxiliary['computers'] = self.computers

    def process_to_celery(self, task: Task, queue: str, computer: dict):
        r = execute.apply_async((task.id,), queue=queue, retry=False)
        task.status = TaskStatus.Queued.value
        task.computer_assigned = computer['name']
        task.celery_id = r.id

        if task.gpu_assigned is not None:
            for g in map(int, task.gpu_assigned.split(',')):
                computer['gpu'][g] = task.id
            computer['cpu'] -= task.cpu
            computer['memory'] -= task.memory * 1024

        self.logger.info(
            f'Sent task={task.id} to celery. Queue = {queue} '
            f'Task status = {task.status} Celery_id = {r.id}',
            ComponentType.Supervisor)
        self.provider.update()

    def create_service_task(
            self,
            task: Task,
            gpu_assigned=None,
            distr_info: dict = None,
            resume: dict = None
    ):
        new_task = Task(
            name=task.name,
            computer=task.computer,
            executor=task.executor,
            status=TaskStatus.NotRan.value,
            type=TaskType.Service.value,
            gpu_assigned=gpu_assigned,
            parent=task.id,
            report=task.report,
            dag=task.dag
        )
        new_task.additional_info = task.additional_info

        if distr_info:
            additional_info = yaml_load(new_task.additional_info)
            additional_info['distr_info'] = distr_info
            new_task.additional_info = yaml_dump(additional_info)

        if resume:
            additional_info = yaml_load(new_task.additional_info)
            additional_info['resume'] = resume
            new_task.additional_info = yaml_dump(additional_info)

        return self.provider.add(new_task)

    def find_port(self, c: dict, docker_name: str):
        docker = self.docker_provider.get(c['name'], docker_name)
        ports = list(map(int, docker.ports.split('-')))
        for p in range(ports[0], ports[1] + 1):
            if p not in c['ports']:
                return p
        raise Exception(f'All ports in {c["name"]} are taken')

    def _process_task_valid_computer(self, task: Task, c: dict,
                                     single_node: bool):
        if not c['can_process_tasks']:
            return 'this computer can not process tasks'

        if task.computer is not None and task.computer != c['name']:
            return 'name set in the config!= name of this computer'

        if task.cpu > c['cpu']:
            return f'task cpu = {task.cpu} > computer' \
                   f' free cpu = {c["cpu"]}'

        if task.memory > c['memory']:
            return f'task cpu = {task.cpu} > computer ' \
                   f'free memory = {c["memory"]}'

        queue = f'{c["name"]}_' \
                f'{task.dag_rel.docker_img or "default"}'
        if queue not in self.queues:
            return f'required queue = {queue} not in queues'

        if task.gpu > 0 and not any(g == 0 for g in c['gpu']):
            return f'task requires gpu, but there is not any free'

        free_gpu = sum(g == 0 for g in c['gpu'])
        if single_node and task.gpu > free_gpu:
            return f'task requires {task.gpu} ' \
                   f'but there are only {free_gpu} free'

    def _process_task_get_computers(
            self, executor: dict, task: Task, auxiliary: dict
    ):
        single_node = executor.get('single_node', True)

        computers = []
        for c in self.computers:
            error = self._process_task_valid_computer(task, c, single_node)
            auxiliary['computers'].append({'name': c['name'], 'error': error})
            if not error:
                computers.append(c)

        if task.gpu > 0 and single_node and len(computers) > 0:
            computers = sorted(
                computers,
                key=lambda x: sum(g == 0 for g in c['gpu']),
                reverse=True
            )[:1]

        free_gpu = sum(sum(g == 0 for g in c['gpu']) for c in computers)
        if task.gpu > free_gpu:
            auxiliary['not_valid'] = f'gpu required by the ' \
                                     f'task = {task.gpu},' \
                                     f' but there are only {free_gpu} ' \
                                     f'free gpus'
            return []
        return computers

    def _process_task_to_send(
            self, executor: dict, task: Task, computers: List[dict]
    ):
        distr = executor.get('distr', True)
        to_send = []
        for computer in computers:
            queue = f'{computer["name"]}_' \
                    f'{task.dag_rel.docker_img or "default"}'

            if task.gpu_max > 1 and distr:
                for index, task_taken_gpu in enumerate(computer['gpu']):
                    if task_taken_gpu:
                        continue
                    to_send.append([computer, queue, index])

                    if len(to_send) >= task.gpu_max:
                        break

                if len(to_send) >= task.gpu_max:
                    break
            elif task.gpu_max > 0:
                cuda_devices = []
                for index, task_taken_gpu in enumerate(computer['gpu']):
                    if task_taken_gpu:
                        continue

                    cuda_devices.append(index)
                    if len(cuda_devices) >= task.gpu_max:
                        break

                task.gpu_assigned = ','.join(map(str, cuda_devices))
                self.process_to_celery(task, queue, computer)
            else:
                self.process_to_celery(task, queue, computer)
                break
        return to_send

    def process_task(self, task: Task):
        auxiliary = self.auxiliary['process_tasks'][-1]
        auxiliary['computers'] = []

        config = yaml_load(task.dag_rel.config)
        executor = config['executors'][task.executor]

        computers = self._process_task_get_computers(executor, task, auxiliary)
        if len(computers) == 0:
            return

        to_send = self._process_task_to_send(executor, task, computers)
        auxiliary['to_send'] = to_send
        additional_info = yaml_load(task.additional_info)

        rank = 0
        master_port = None
        if len(to_send) > 0:

            master_port = self.find_port(
                to_send[0][0], to_send[0][1].split('_')[1]
            )
            computer_names = {c['name'] for c, _, __ in to_send}
            if len(computer_names) == 1:
                task.computer_assigned = list(computer_names)[0]

        for computer, queue, gpu_assigned in to_send:
            main_cmp = to_send[0][0]
            # noinspection PyTypeChecker
            ip = 'localhost' if computer['name'] == main_cmp['name'] \
                else main_cmp['ip']

            distr_info = {
                'master_addr': ip,
                'rank': rank,
                'local_rank': gpu_assigned,
                'master_port': master_port,
                'world_size': len(to_send),
                'master_computer': main_cmp['name']
            }
            service_task = self.create_service_task(
                task,
                distr_info=distr_info,
                gpu_assigned=gpu_assigned,
                resume=additional_info.get('resume')
            )
            self.process_to_celery(service_task, queue, computer)
            rank += 1
            main_cmp['ports'].add(master_port)

        if len(to_send) > 0:
            task.status = TaskStatus.Queued.value
            self.provider.commit()

    def process_tasks(self):
        self.auxiliary['process_tasks'] = []

        for task in self.not_ran_tasks:
            auxiliary = {'id': task.id, 'name': task.name}
            self.auxiliary['process_tasks'].append(auxiliary)

            if task.dag_rel is None:
                task.dag_rel = self.dag_provider.by_id(task.dag)

            if TaskStatus.Stopped.value in self.dep_status[task.id] \
                    or TaskStatus.Failed.value in self.dep_status[task.id] or \
                    TaskStatus.Skipped.value in self.dep_status[task.id]:
                auxiliary['not_valid'] = 'stopped or failed in dep_status'
                self.provider.change_status(task, TaskStatus.Skipped)
                continue

            if len(self.dep_status[task.id]) != 0 \
                    and self.dep_status[task.id] != {TaskStatus.Success.value}:
                auxiliary['not_valid'] = 'not all dep tasks are finished'
                continue
            self.process_task(task)

    def _stop_child_tasks(self, task: Task):
        self.provider.commit()

        children = self.provider.children(task.id, [Task.dag_rel])
        dags = [c.dag_rel for c in children]
        for c, d in zip(children, dags):
            celery_tasks.stop(self.logger, self.session, c, d)

    def process_parent_tasks(self):
        tasks = self.provider.parent_tasks_stats()

        was_change = False
        for task, started, finished, statuses in tasks:
            status = task.status
            if statuses[TaskStatus.Failed] > 0:
                status = TaskStatus.Failed.value
            elif statuses[TaskStatus.Skipped] > 0:
                status = TaskStatus.Skipped.value
            elif statuses[TaskStatus.Queued] > 0:
                status = TaskStatus.Queued.value
            elif statuses[TaskStatus.InProgress] > 0:
                status = TaskStatus.InProgress.value
            elif statuses[TaskStatus.Success] > 0:
                status = TaskStatus.Success.value

            if status != task.status:
                if status == TaskStatus.InProgress.value:
                    task.started = started
                elif status >= TaskStatus.Failed.value:
                    task.started = started
                    task.finished = finished
                    self._stop_child_tasks(task)

                was_change = True
                task.status = status

        if was_change:
            self.provider.commit()

        self.auxiliary['parent_tasks_stats'] = [
            {
                'name': task.name,
                'id': task.id,
                'started': task.started,
                'finished': finished,
                'statuses': [
                    {
                        'name': k.name,
                        'count': v
                    } for k, v in statuses.items()
                ],
            } for task, started, finished, statuses in tasks
        ]

    def write_auxiliary(self):
        self.auxiliary['duration'] = (now() - self.auxiliary['time']). \
            total_seconds()

        auxiliary = Auxiliary(
            name='supervisor', data=yaml_dump(self.auxiliary)
        )
        self.auxiliary_provider.create_or_update(auxiliary, 'name')

    def build(self):
        try:
            self.auxiliary = {'time': now()}

            self.create_base()

            self.process_parent_tasks()

            self.load_tasks()

            self.load_computers()

            self.process_tasks()

            self.write_auxiliary()

        except ObjectDeletedError:
            pass
        except Exception as e:
            if Session.sqlalchemy_error(e):
                Session.cleanup(key='SupervisorBuilder')
                self.session = Session.create_session(key='SupervisorBuilder')
                self.logger = create_logger(self.session, 'SupervisorBuilder')

            self.logger.error(traceback.format_exc(), ComponentType.Supervisor)


def register_supervisor():
    builder = SupervisorBuilder()
    start_schedule([(builder.build, 1)])
