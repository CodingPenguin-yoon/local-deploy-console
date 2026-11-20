import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app

class DeploymentTests(unittest.TestCase):
    def test_image_yaml(self):
        self.assertEqual(app.image_from_yaml('image:\n  repository: registry/team/api\n  tag: "abc123"'), 'registry/team/api:abc123')
        for tag in ['latest', 'not-built-yet', 'bad;command']:
            with self.assertRaises(ValueError):
                app.image_from_yaml(f'image:\n  repository: registry/team/api\n  tag: "{tag}"')

    def target(self):
        return dict(id='test',name='test',context='team',namespace='team',deployment='api',container='api',current='r/api:old',desired='r/api:new',commit='new',auto=True)

    def test_image_only_and_explicit_scope(self):
        t=self.target()
        live='{"spec":{"template":{"spec":{"containers":[{"name":"api","image":"r/api:new"}]}}}}'
        with tempfile.TemporaryDirectory() as d, patch.object(app,'STATE',Path(d)), patch.object(app,'DATA',{'targets':[t],'history':[]}), patch.object(app,'run',side_effect=['yes','','',live]) as run:
            app.deploy(t)
            commands=[call.args[0] for call in run.call_args_list][1:]
            self.assertIn('api=r/api:new',commands[0])
            self.assertEqual(commands[0][1:5],['--context','team','--namespace','team'])
            self.assertNotIn('apply',sum(commands,[]))
            self.assertEqual(t['last_success_commit'],'new')

    def test_failure_disables_auto_without_marking_success(self):
        t=self.target()
        with tempfile.TemporaryDirectory() as d, patch.object(app,'STATE',Path(d)), patch.object(app,'DATA',{'targets':[t],'history':[]}), patch.object(app,'inspect'), patch.object(app,'run',side_effect=RuntimeError('rollout failed')):
            app.operate(t,'poll')
            self.assertFalse(t['auto'])
            self.assertNotIn('last_success_commit',t)

    def test_retry_with_matching_image_still_requires_rollout(self):
        t = self.target()
        t['current'] = t['desired']
        with patch.object(app, 'inspect'), patch.object(app, 'save'), patch.object(app, 'DATA', {'targets': [t], 'history': []}), patch.object(app, 'run', side_effect=RuntimeError('rollout failed')) as run:
            app.operate(t, 'poll')
            self.assertEqual(run.call_count, 1)
            self.assertIn('rollout', run.call_args.args[0])
            self.assertFalse(t['auto'])
            self.assertNotIn('last_success_commit', t)

    def test_successful_commit_not_redeployed_on_drift(self):
        t=self.target(); t['last_success_commit']='new'
        with tempfile.TemporaryDirectory() as d, patch.object(app,'STATE',Path(d)), patch.object(app,'DATA',{'targets':[t],'history':[]}), patch.object(app,'inspect'), patch.object(app,'deploy') as deploy:
            app.operate(t,'poll')
            deploy.assert_not_called()

if __name__=='__main__': unittest.main()
