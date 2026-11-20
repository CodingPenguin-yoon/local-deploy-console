import unittest
import yaml
import charts

FIXTURE = '''
kind: Deployment
metadata: {name: web}
spec:
  template:
    metadata: {labels: {app: web}}
    spec:
      containers: [{name: web, image: 'registry/web:abc123'}]
---
kind: StatefulSet
metadata: {name: redis}
spec:
  template:
    metadata: {labels: {app: redis}}
    spec:
      containers: [{name: redis, image: 'redis:7.4.12-alpine'}]
      volumes: [{name: data, persistentVolumeClaim: {claimName: redis-data}}]
---
kind: Service
metadata: {name: web-svc}
spec: {selector: {app: web}, ports: [{port: 80}]}
---
kind: Service
metadata: {name: wrong-svc}
spec: {selector: {app: other}, ports: [{port: 90}]}
---
kind: Ingress
metadata: {name: web-ingress}
spec:
  rules:
    - host: example.test
      http:
        paths: [{path: /, backend: {service: {name: web-svc, port: {number: 80}}}}]
---
kind: Secret
metadata: {name: hidden}
stringData: {password: never-return}
'''

class ChartTests(unittest.TestCase):
    def test_multiple_workloads_and_selector_links(self):
        result = charts.workloads(yaml.safe_load_all(FIXTURE), 'team')
        self.assertEqual([w['deployment'] for w in result], ['web', 'redis'])
        self.assertEqual([r['name'] for r in result[0]['related']], ['web-svc', 'web-ingress'])
        self.assertEqual(result[1]['related'][0]['name'], 'redis-data')
        self.assertNotIn('never-return', str(result))

    def test_foreign_namespace_is_rejected(self):
        doc = next(yaml.safe_load_all(FIXTURE))
        doc['metadata']['namespace'] = 'another-team'
        with self.assertRaises(ValueError): charts.workloads([doc], 'team')

    def test_cross_namespace_service_not_linked(self):
        docs = list(yaml.safe_load_all(FIXTURE))
        docs[2]['metadata']['namespace'] = 'other'
        self.assertEqual(charts.workloads(docs, 'team')[0]['related'], [])

    def test_unpinned_images_and_traversal_rejected(self):
        for image in ['redis', 'redis:latest', 'repo/api:not-built-yet']:
            with self.assertRaises(ValueError): charts.image_check(image)
        for path in ['../secret', '/tmp/chart', 'x/../../foo']:
            with self.assertRaises(ValueError): charts.relative_path(path)
