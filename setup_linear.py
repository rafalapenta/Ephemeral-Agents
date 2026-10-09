import os, json, urllib.request
from pathlib import Path
from dotenv import dotenv_values

env_path = Path('.env')
content = env_path.read_bytes().decode('utf-8')
content = content.replace('\r\n', '\n').replace('\n', '\r\n')
env_path.write_bytes(content.encode('utf-8'))
print('Normalized .env to CRLF')

cfg = dotenv_values('.env')
key = cfg.get('LINEAR_API_KEY')
team = cfg.get('LINEAR_TEAM_ID', 'EPH')

q = '''query($k:String!){ teams(filter:{key:{eq:$k}}){ nodes{ id states{ nodes{ id name type } } } } }'''
req = urllib.request.Request('https://api.linear.app/graphql', method='POST', data=json.dumps({'query': q, 'variables': {'k': team}}).encode(), headers={'Authorization': key, 'Content-Type': 'application/json'})
res = json.loads(urllib.request.urlopen(req).read())
team_node = res['data']['teams']['nodes'][0]
team_id = team_node['id']
todo_state = next(s['id'] for s in team_node['states']['nodes'] if s['name'].lower() == 'todo' or s['type'] == 'unstarted')

mut = '''mutation($teamId: String!, $title: String!, $stateId: String!) { issueCreate(input: {teamId: $teamId, title: $title, stateId: $stateId}) { issue { identifier title } } }'''
req = urllib.request.Request('https://api.linear.app/graphql', method='POST', data=json.dumps({'query': mut, 'variables': {'teamId': team_id, 'title': 'E2E Orchestrator Handoff Test', 'stateId': todo_state}}).encode(), headers={'Authorization': key, 'Content-Type': 'application/json'})
issue = json.loads(urllib.request.urlopen(req).read())
print('Created Linear Issue:', issue['data']['issueCreate']['issue']['identifier'])
