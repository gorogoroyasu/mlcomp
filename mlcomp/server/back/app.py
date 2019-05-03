from flask import Flask, request
import json
import click
import requests
from mlcomp.db.providers import *
from mlcomp.db.core import PaginatorOptions
from flask_cors import CORS
from mlcomp.server.back.supervisor import register_supervisor
import os

PORT = 4201

app = Flask(__name__)
CORS(app)


def parse_int(args: dict, key: str):
    return int(args[key]) if args.get(key) and args[key].isnumeric() else None


def construct_paginator_options(args: dict, default_sort_column: str):
    return PaginatorOptions(sort_column=args.get('sort_column') or default_sort_column,
                            sort_descending=args['sort_descending'] == 'true' if 'sort_descending' in args else True,
                            page_number=parse_int(args, 'page_number'),
                            page_size=parse_int(args, 'page_size'),
                            )


@app.route('/projects')
def projects():
    options = construct_paginator_options(request.args, 'id')

    provider = ProjectProvider()
    res = provider.get(options)
    return json.dumps(res)


def get_dag_id():
    assert 'dag_id' in request.args, 'dag_id is needed'
    assert request.args['dag_id'].isnumeric(), 'dag_id must be integer'
    return int(request.args['dag_id'])


@app.route('/config')
def config():
    id = get_dag_id()
    res = DagProvider().config(id)
    return json.dumps({'data': res})


@app.route('/dags')
def dags():
    options = construct_paginator_options(request.args, 'id')
    project = parse_int(request.args, 'project')
    provider = DagProvider()
    res = provider.get(project, options)
    return json.dumps(res)


@app.route('/code')
def code():
    id = get_dag_id()
    res = OrderedDict()
    parents = dict()
    for s, f in DagStorageProvider().by_dag(id):
        s.path = s.path.strip()
        parent = os.path.dirname(s.path)
        name = os.path.basename(s.path)
        if name == '':
            continue

        if s.is_dir:
            node = {'name': name, 'children': []}
            if not parent:
                res[name] = node
                parents[s.path] = res[name]
            else:
                parents[parent]['children'].append(node)
        else:
            node = {'name': name, 'content': f.content.decode('utf-8')}
            if not parent:
                res[name] = node
            else:
                parents[parent]['children'].append(node)

    return json.dumps(list(res.values()))


@app.route('/tasks')
def tasks():
    provider = TaskProvider()
    options = construct_paginator_options(request.args, 'id')
    res = provider.get(options)
    return json.dumps(res)


def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()


@app.route('/shutdown', methods=['POST'])
def shutdown():
    shutdown_server()
    return 'Server shutting down...'


@click.group()
def base():
    pass


@base.command()
def start():
    register_supervisor()
    app.run(debug=True, port=PORT)


@base.command()
def stop():
    requests.post(f'http://localhost:{PORT}/shutdown')


if __name__ == '__main__':
    # files(30)
    base()
