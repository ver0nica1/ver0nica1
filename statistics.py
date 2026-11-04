"""Actualiza las estadísticas de GitHub que se muestran en dark_mode.svg y light_mode.svg.

Variables de entorno necesarias:
    ACCESS_TOKEN: token fine-grained de solo lectura con acceso a todos los repositorios.
        Account permissions: Followers (read)
        Repository permissions: Contents (read), Metadata (read)
    USER_NAME: usuario de GitHub (por ejemplo, 'ver0nica1').
"""
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
from dateutil.relativedelta import relativedelta
from lxml import etree

API_URL = 'https://api.github.com/graphql'
REQUEST_TIMEOUT = 30  # segundos
BIRTHDAY = datetime.datetime(2001, 6, 25)
SVG_FILES = ('dark_mode.svg', 'light_mode.svg')
# El caché se guarda entre ejecuciones con actions/cache (ver el workflow).
CACHE_FILE = Path('cache/loc_cache.json')

# id del elemento en el SVG -> ancho total (valor + puntos) para alinear la columna.
# Ancho 0 significa que ese valor no tiene puntos de relleno.
SVG_FIELDS = {
    'age_data': 49,
    'repo_data': 6,
    'contrib_data': 0,
    'star_data': 14,
    'commit_data': 21,
    'follower_data': 10,
    'loc_data': 8,
    'loc_add': 0,
    'loc_del': 7,
}

USER_QUERY = '''
query($login: String!) {
    user(login: $login) {
        id
        followers { totalCount }
    }
}'''

# Lista todos los repos (propios y en los que colaboras) y cuántos commits tuyos tiene
# cada uno en la rama principal. El filtro `author` hace que GitHub cuente solo tus commits.
REPOS_QUERY = '''
query($login: String!, $authorId: ID!, $cursor: String) {
    user(login: $login) {
        repositories(first: 50, after: $cursor,
                     ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
            nodes {
                nameWithOwner
                stargazerCount
                owner { login }
                defaultBranchRef {
                    target {
                        ... on Commit {
                            history(author: {id: $authorId}) { totalCount }
                        }
                    }
                }
            }
            pageInfo { endCursor hasNextPage }
        }
    }
}'''

# Líneas añadidas/borradas de tus commits en un repo concreto.
HISTORY_QUERY = '''
query($owner: String!, $name: String!, $authorId: ID!, $cursor: String) {
    repository(owner: $owner, name: $name) {
        defaultBranchRef {
            target {
                ... on Commit {
                    history(first: 100, after: $cursor, author: {id: $authorId}) {
                        nodes { additions deletions }
                        pageInfo { endCursor hasNextPage }
                    }
                }
            }
        }
    }
}'''


class GitHubAPI:
    """Cliente mínimo de la API GraphQL de GitHub que cuenta las llamadas realizadas."""

    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {token}'
        self.calls = 0

    def query(self, query, variables):
        self.calls += 1
        response = self.session.post(API_URL, json={'query': query, 'variables': variables},
                                     timeout=REQUEST_TIMEOUT)
        if response.status_code == 403:
            raise RuntimeError('GitHub rechazó la petición (403): probablemente se alcanzó el límite de uso.')
        response.raise_for_status()
        payload = response.json()
        if payload.get('errors'):
            raise RuntimeError(f"Error de GraphQL: {payload['errors']}")
        return payload['data']

    def paginate(self, query, variables, get_connection):
        """Recorre todas las páginas de una conexión GraphQL y devuelve sus nodos."""
        cursor = None
        while True:
            data = self.query(query, {**variables, 'cursor': cursor})
            connection = get_connection(data)
            if connection is None:  # p. ej. repo vacío, sin rama principal
                return
            yield from connection['nodes']
            if not connection['pageInfo']['hasNextPage']:
                return
            cursor = connection['pageInfo']['endCursor']


def format_age(birthday, today):
    """Devuelve 'X years, X months, X days' desde el cumpleaños (con 🎂 el día del cumpleaños)."""
    diff = relativedelta(today, birthday)

    def unit(value, name):
        return f"{value} {name}{'' if value == 1 else 's'}"

    age = ', '.join((unit(diff.years, 'year'), unit(diff.months, 'month'), unit(diff.days, 'day')))
    return age + (' 🎂' if diff.months == 0 and diff.days == 0 else '')


def default_branch_history(repo):
    """Devuelve el nodo `history` de la rama principal, o None si el repo está vacío."""
    branch = repo['defaultBranchRef']
    return branch['target']['history'] if branch else None


def repo_key(name_with_owner):
    # Se guarda el hash en lugar del nombre para no exponer repos privados.
    return hashlib.sha256(name_with_owner.encode('utf-8')).hexdigest()


def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding='utf-8')


def count_lines(api, name_with_owner, author_id):
    owner, name = name_with_owner.split('/')
    commits = api.paginate(
        HISTORY_QUERY,
        {'owner': owner, 'name': name, 'authorId': author_id},
        lambda data: default_branch_history(data['repository']),
    )
    additions = deletions = 0
    for commit in commits:
        additions += commit['additions']
        deletions += commit['deletions']
    return additions, deletions


def collect_stats(api, login):
    user = api.query(USER_QUERY, {'login': login})['user']
    author_id = user['id']

    repos = list(api.paginate(
        REPOS_QUERY,
        {'login': login, 'authorId': author_id},
        lambda data: data['user']['repositories'],
    ))
    owned = [repo for repo in repos if repo['owner']['login'].lower() == login.lower()]

    old_cache, new_cache = load_cache(), {}
    for repo in repos:
        key = repo_key(repo['nameWithOwner'])
        history = default_branch_history(repo)
        my_commits = history['totalCount'] if history else 0

        entry = old_cache.get(key)
        # Solo se vuelven a contar las líneas si cambió el número de commits tuyos.
        if entry is None or entry['commits'] != my_commits:
            additions, deletions = count_lines(api, repo['nameWithOwner'], author_id) if my_commits else (0, 0)
            entry = {'commits': my_commits, 'additions': additions, 'deletions': deletions}
        new_cache[key] = entry
    save_cache(new_cache)  # los repos que ya no existen desaparecen del caché

    additions = sum(entry['additions'] for entry in new_cache.values())
    deletions = sum(entry['deletions'] for entry in new_cache.values())
    return {
        'age_data': format_age(BIRTHDAY, datetime.datetime.now()),
        'repo_data': len(owned),
        'contrib_data': len(repos),
        'star_data': sum(repo['stargazerCount'] for repo in owned),
        'commit_data': sum(entry['commits'] for entry in new_cache.values()),
        'follower_data': user['followers']['totalCount'],
        'loc_data': additions - deletions,
        'loc_add': additions,
        'loc_del': deletions,
    }


def dots(value_length, width):
    """Relleno de puntos que alinea el valor dentro de `width` caracteres."""
    padding = max(0, width - value_length)
    if padding <= 2:
        return ('', ' ', '. ')[padding]
    return ' ' + '.' * padding + ' '


def update_svg(filename, stats):
    tree = etree.parse(filename)
    elements = {el.get('id'): el for el in tree.getroot().iter() if el.get('id')}

    for element_id, width in SVG_FIELDS.items():
        value = stats[element_id]
        text = f'{value:,}' if isinstance(value, int) else str(value)
        if element_id not in elements:
            print(f'Aviso: {filename} no tiene el elemento id="{element_id}"', file=sys.stderr)
            continue
        elements[element_id].text = text
        if width and f'{element_id}_dots' in elements:
            elements[f'{element_id}_dots'].text = dots(len(text), width)

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def main():
    token, login = os.environ.get('ACCESS_TOKEN'), os.environ.get('USER_NAME')
    if not token or not login:
        sys.exit('Faltan las variables de entorno ACCESS_TOKEN y/o USER_NAME.')

    start = time.perf_counter()
    api = GitHubAPI(token)
    stats = collect_stats(api, login)
    for filename in SVG_FILES:
        update_svg(filename, stats)

    # No se imprimen nombres de repos: los logs de Actions son públicos.
    for key, value in stats.items():
        print(f'{key:<15} {value}')
    print(f'\nLlamadas a la API: {api.calls} | Tiempo total: {time.perf_counter() - start:.2f} s')


if __name__ == '__main__':
    main()
