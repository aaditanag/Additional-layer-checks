import json, sys, os, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'layer_2'))
try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

class MockWS:
    def __init__(self):
        self.sent = []
        self.closed = False
    async def send_str(self, s):
        self.sent.append(s)
    def last_msg(self):
        return json.loads(self.sent[-1]) if self.sent else None

@unittest.skipUnless(AIOHTTP_OK, 'aiohttp not installed')
class TestServer(unittest.IsolatedAsyncioTestCase):
    async def _get_srv(self):
        m = 'server'
        if m in sys.modules:
            del sys.modules[m]
        import server as srv
        srv._seen_event_ids.clear()
        srv.ml_evaluation_log.clear()
        srv.pi_client = None
        srv.current_manifest = {}
        srv.browser_clients = set()
        return srv

    async def test_B1_pi_offline(self):
        srv = await self._get_srv()
        ws = MockWS()
        await srv.handle_crash_test_requested(ws, {'event_id': 'e1', 'scenario_id': 'front_collision_demo_01', 'mode': 'forced'})
        self.assertEqual(ws.last_msg()['status'], 'pi_offline')

    async def test_B2_empty_id_rejected(self):
        srv = await self._get_srv()
        ws = MockWS()
        await srv.handle_crash_test_requested(ws, {'event_id': '', 'scenario_id': 'front_collision_demo_01', 'mode': 'forced'})
        self.assertEqual(ws.last_msg()['status'], 'rejected')

    async def test_B3_bad_scenario(self):
        srv = await self._get_srv()
        ws = MockWS()
        await srv.handle_crash_test_requested(ws, {'event_id': 'e2', 'scenario_id': 'gpio_attack', 'mode': 'forced'})
        msg = ws.last_msg()
        self.assertEqual(msg['status'], 'rejected')
        self.assertIn('scenario_id', msg.get('reason', ''))

    async def test_B4_wrong_mode(self):
        srv = await self._get_srv()
        ws = MockWS()
        await srv.handle_crash_test_requested(ws, {'event_id': 'e3', 'scenario_id': 'front_collision_demo_01', 'mode': 'auto'})
        self.assertEqual(ws.last_msg()['status'], 'rejected')

    async def test_B5_duplicate_rejected(self):
        srv = await self._get_srv()
        d = {'event_id': 'e4', 'scenario_id': 'front_collision_demo_01', 'mode': 'forced'}
        ws1 = MockWS()
        await srv.handle_crash_test_requested(ws1, d)
        ws2 = MockWS()
        await srv.handle_crash_test_requested(ws2, d)
        msg = ws2.last_msg()
        self.assertEqual(msg['status'], 'rejected')
        self.assertIn('Duplicate', msg.get('reason', ''))

    async def test_B6_forwarded_no_gpio(self):
        srv = await self._get_srv()
        pi_ws = MockWS()
        srv.pi_client = pi_ws
        bws = MockWS()
        await srv.handle_crash_test_requested(bws, {'event_id': 'e5', 'scenario_id': 'front_collision_demo_01', 'mode': 'forced'})
        self.assertEqual(bws.last_msg()['status'], 'forwarded_to_pi')
        pi_msg = pi_ws.last_msg()
        self.assertEqual(pi_msg['type'], 'crash_test_requested')
        self.assertNotIn('gpio_pin', pi_msg)

    async def test_D1_ml_marker(self):
        srv = await self._get_srv()
        ws = MockWS()
        srv.browser_clients = {ws}
        await srv.handle_ml_evaluation_marker(ws, {'event_id': 'm1', 'scenario_id': 'front_collision_demo_01'})
        # ws receives both: ml_marker_ack (first) and ml_evaluation_marker broadcast (second)
        all_msgs = ws.all_msgs() if hasattr(ws, 'all_msgs') else [json.loads(s) for s in ws.sent]
        ack_msgs = [m for m in all_msgs if m.get('type') == 'ml_marker_ack']
        self.assertTrue(len(ack_msgs) > 0, 'Expected ml_marker_ack message')
        msg = ack_msgs[0]
        self.assertEqual(msg['type'], 'ml_marker_ack')
        self.assertEqual(msg['status'], 'recorded')
        self.assertIn('NOT an ML prediction', srv.ml_evaluation_log[0]['note'])

    async def test_D2_manifest_crash_ts(self):
        srv = await self._get_srv()
        ws = MockWS()
        srv.browser_clients = {ws}
        srv.current_manifest = {'timeline': {'crash_ms': 33000}}
        await srv.handle_ml_evaluation_marker(ws, {'event_id': 'm2', 'scenario_id': 'front_collision_demo_01'})
        self.assertEqual(ws.last_msg()['manifest_crash_ms'], 33000)

    async def test_D3_duplicate_marker(self):
        srv = await self._get_srv()
        ws = MockWS()
        srv.browser_clients = {ws}
        d = {'event_id': 'm3', 'scenario_id': 'front_collision_demo_01'}
        await srv.handle_ml_evaluation_marker(ws, d)
        ws2 = MockWS()
        srv.browser_clients = {ws2}
        await srv.handle_ml_evaluation_marker(ws2, d)
        self.assertEqual(ws2.last_msg()['status'], 'rejected')

    async def test_E1_dedup_ttl(self):
        srv = await self._get_srv()
        srv._seen_event_ids['old'] = time.monotonic() - 400
        self.assertFalse(srv._is_duplicate_event('old'))
        self.assertTrue(srv._is_duplicate_event('old'))


class TestActuatorSafety(unittest.TestCase):
    def _get(self, **kw):
        sys.path.insert(0, os.path.join(ROOT, 'layer_pi', 'l4'))
        if 'actuator' in sys.modules:
            del sys.modules['actuator']
        from actuator import ActuatorEngine
        return ActuatorEngine(hardware=False, **kw)

    def test_C1_default_disarmed(self):
        self.assertFalse(self._get().airbag_armed)

    def test_C2_deploy_blocked(self):
        a = self._get(airbag_armed=False)
        r = a.deploy_airbag('T')
        self.assertTrue(r)
        self.assertTrue(a.deployed)
        self.assertFalse(a.gpio_ready)

    def test_C3_one_shot_lock(self):
        a = self._get()
        self.assertTrue(a.deploy_airbag('F1'))
        self.assertFalse(a.deploy_airbag('F2'))
        self.assertEqual(a.deploy_reason, 'F1')

    def test_C4_new_instance_disarmed(self):
        a1 = self._get()
        a1.deployed = True
        a2 = self._get()
        self.assertFalse(a2.airbag_armed)
        self.assertFalse(a2.deployed)

    def test_C5_warning_rate_limit(self):
        a = self._get()
        a._last_pulse = time.time()
        before = a._last_pulse
        a.pulse_warning(n_pulses=1, pulse_ms=50)
        self.assertAlmostEqual(a._last_pulse, before, places=1)


class TestManifest(unittest.TestCase):
    def _load(self):
        with open(os.path.join(ROOT, 'data', 'scenario_manifest.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_scenario_id(self):
        self.assertEqual(self._load()['scenario_id'], 'front_collision_demo_01')

    def test_timeline(self):
        m = self._load()
        tl = m['timeline']
        self.assertIn('crash_ms', tl)
        self.assertGreater(tl['crash_ms'], tl['normal_start_ms'])

    def test_crash_known_at(self):
        m = self._load()
        self.assertEqual(m['crash_known_at_ms'], m['timeline']['crash_ms'])


class TestCSV(unittest.TestCase):
    def test_columns(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest('pandas missing')
        df = pd.read_csv(os.path.join(ROOT, 'data', 'pre_decided_sensor_data.csv'), nrows=5)
        for c in ['timestamp_ms', 'ax', 'ay', 'az', 'gx', 'gy', 'gz', 'hg_ax', 'hg_ay', 'hg_az', 'label']:
            self.assertIn(c, df.columns)

    def test_crash_rows(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest('pandas missing')
        df = pd.read_csv(os.path.join(ROOT, 'data', 'pre_decided_sensor_data.csv'))
        self.assertGreater(len(df[df['label'] == 2]), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
