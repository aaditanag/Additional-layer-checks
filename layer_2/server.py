import asyncio, json, os, sys, time, logging
from pathlib import Path
from aiohttp import web
import aiohttp

log = logging.getLogger('server')

ROOT          = Path(__file__).parent.parent
LAYER2        = Path(__file__).parent
CSV_PATH      = ROOT / 'data' / 'pre_decided_sensor_data.csv'
MANIFEST_PATH = ROOT / 'data' / 'scenario_manifest.json'

ALLOWED_SCENARIO_IDS = {'front_collision_demo_01', 'existing'}
PLAYBACK_FPS  = 20
FRAME_STEP    = 10
DEDUP_TTL_S   = 300

browser_clients = set()
browser_ready   = False
pi_client       = None
playback_task   = None
current_df      = None
current_meta    = {}
current_manifest = {}
_seen_event_ids = {}
ml_evaluation_log = []

def _is_duplicate_event(event_id):
    now = time.monotonic()
    expired = [k for k, t in _seen_event_ids.items() if now - t > DEDUP_TTL_S]
    for k in expired:
        del _seen_event_ids[k]
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids[event_id] = now
    return False

async def handle_index(request):
    return web.FileResponse(LAYER2 / 'cockpit.html')

async def handle_dashboard(request):
    return web.FileResponse(LAYER2 / 'cockpit.html')

async def handle_static(request):
    fname = request.match_info['name']
    path  = LAYER2 / fname
    if path.exists():
        return web.FileResponse(path)
    return web.Response(status=404, text='Not found')

async def handle_manifest(request):
    if MANIFEST_PATH.exists():
        return web.FileResponse(MANIFEST_PATH)
    return web.json_response({'error': 'No manifest found'}, status=404)

async def handle_generate(request):
    try:
        data   = await request.json()
        prompt = data.get('prompt', '').strip()
        biome  = data.get('biome', 'hill_station')
        mode   = data.get('mode', 'llm')
        events = data.get('events', [])
        if mode == 'llm' and prompt:
            cmd = [sys.executable, str(ROOT / 'layer_1' / 'generate_scenario.py'), '--prompt', prompt]
        elif mode == 'proc' and events:
            events_arg = ','.join(f"{e['event']}:{e['duration_ms']}" for e in events)
            cmd = [sys.executable, str(ROOT / 'layer_1' / 'generate_scenario.py'), '--events', events_arg]
        else:
            return web.json_response({'success': True, 'csv_path': str(CSV_PATH), 'biome': biome, 'scenario': 'existing', 'duration_ms': 5000})
        log.info(f'[SERVER] Running Layer 1: {chr(32).join(cmd)}')
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            import pandas as pd
            df = pd.read_csv(str(CSV_PATH))
            dur = float(df['timestamp_ms'].max()) if 'timestamp_ms' in df.columns else len(df)
            return web.json_response({'success': True, 'csv_path': str(CSV_PATH), 'biome': biome, 'scenario': prompt or (','.join(e['event'] for e in events)), 'duration_ms': dur})
        else:
            errmsg = stderr.decode(errors='replace')
            log.warning(f'[SERVER] Layer 1 error: {errmsg}')
            return web.json_response({'success': True, 'csv_path': str(CSV_PATH), 'biome': biome, 'scenario': prompt, 'duration_ms': 5000, 'warning': 'Used existing data'})
    except asyncio.TimeoutError:
        return web.json_response({'success': False, 'error': 'Layer 1 timed out'}, status=504)
    except Exception as ex:
        return web.json_response({'success': False, 'error': str(ex)}, status=500)

async def broadcast(msg):
    dead = set()
    for ws in list(browser_clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    browser_clients.difference_update(dead)

async def playback_csv():
    global current_df, pi_client, browser_ready, current_manifest
    if current_df is None:
        return
    has_phase_col = 'phase' in current_df.columns
    def get_phase(row):
        if has_phase_col:
            return str(row.get('phase', 'Normal'))
        lbl = int(row.get('label', 0))
        return ['Normal', 'Near-Crash', 'Crash'][lbl] if lbl < 3 else 'Normal'
    for _ in range(50):
        if browser_ready:
            break
        await asyncio.sleep(0.1)
    if not browser_ready:
        log.warning('[SERVER] browser_ready not received, starting anyway')
    await asyncio.sleep(0.3)
    interval = 1.0 / PLAYBACK_FPS
    rows = len(current_df)
    has_ts = 'timestamp_ms' in current_df.columns
    log.info(f'[SERVER] Playback: {rows:,} rows -> {rows//FRAME_STEP} frames @ {PLAYBACK_FPS}fps (~{rows//FRAME_STEP/PLAYBACK_FPS:.1f}s)')
    for i in range(0, rows, FRAME_STEP):
        row  = current_df.iloc[i]
        t_ms = float(row['timestamp_ms']) if has_ts else float(i)
        frame = {
            'type': 'sensor_frame', 't_ms': t_ms,
            'label': int(row.get('label', 0)), 'phase': get_phase(row),
            'ax': float(row['ax']), 'ay': float(row['ay']), 'az': float(row['az']),
            'gx': float(row['gx']), 'gy': float(row['gy']), 'gz': float(row['gz']),
            'hg_ax': float(row['hg_ax']), 'hg_ay': float(row['hg_ay']), 'hg_az': float(row['hg_az']),
            'progress': round(i / rows, 4),
        }
        raw = json.dumps(frame)
        await broadcast(raw)
        if pi_client and not pi_client.closed:
            try:
                await pi_client.send_str(raw)
            except Exception:
                pi_client = None
        await asyncio.sleep(interval)
    done = json.dumps({'type': 'playback_done'})
    await broadcast(done)
    if pi_client and not pi_client.closed:
        try:
            await pi_client.send_str(done)
        except Exception:
            pi_client = None
    log.info('[SERVER] Playback complete.')

async def handle_crash_test_requested(ws, data):
    event_id    = data.get('event_id', '')
    scenario_id = data.get('scenario_id', '')
    mode        = data.get('mode', '')
    def reject(reason):
        return json.dumps({'type': 'crash_test_status', 'event_id': event_id, 'status': 'rejected', 'reason': reason})
    if not event_id or len(event_id) > 128:
        await ws.send_str(reject('Invalid or missing event_id'))
        return
    if scenario_id not in ALLOWED_SCENARIO_IDS:
        await ws.send_str(reject(f'Unsupported scenario_id: {repr(scenario_id)}'))
        return
    if mode != 'forced':
        await ws.send_str(reject(f"mode must be 'forced', got: {repr(mode)}"))
        return
    if _is_duplicate_event(event_id):
        await ws.send_str(reject('Duplicate event_id - already processed'))
        return
    if not pi_client or pi_client.closed:
        await ws.send_str(json.dumps({'type': 'crash_test_status', 'event_id': event_id, 'status': 'pi_offline', 'reason': 'Raspberry Pi not connected - visual crash only, no hardware output'}))
        log.info(f'[SERVER] crash_test {event_id} cannot forward - Pi offline')
        return
    forward = json.dumps({'type': 'crash_test_requested', 'event_id': event_id, 'scenario_id': scenario_id, 'mode': 'forced'})
    try:
        await pi_client.send_str(forward)
        await ws.send_str(json.dumps({'type': 'crash_test_status', 'event_id': event_id, 'status': 'forwarded_to_pi', 'reason': 'Event validated and forwarded to Pi'}))
        log.info(f'[SERVER] crash_test {event_id} forwarded to Pi')
    except Exception as e:
        await ws.send_str(json.dumps({'type': 'crash_test_status', 'event_id': event_id, 'status': 'pi_error', 'reason': str(e)}))

async def handle_ml_evaluation_marker(ws, data):
    event_id    = data.get('event_id', '')
    scenario_id = data.get('scenario_id', '')
    if not event_id or len(event_id) > 128:
        await ws.send_str(json.dumps({'type': 'ml_marker_ack', 'status': 'rejected', 'reason': 'Invalid event_id'}))
        return
    if _is_duplicate_event(event_id):
        await ws.send_str(json.dumps({'type': 'ml_marker_ack', 'event_id': event_id, 'status': 'rejected', 'reason': 'Duplicate marker'}))
        return
    now_ms = time.time() * 1000
    manifest_crash_ms = None
    if current_manifest:
        manifest_crash_ms = current_manifest.get('timeline', {}).get('crash_ms')
    record = {'event_id': event_id, 'scenario_id': scenario_id, 'marker_wall_ms': now_ms, 'manifest_crash_ms': manifest_crash_ms, 'note': 'Ground-truth marker - NOT an ML prediction'}
    ml_evaluation_log.append(record)
    log.info(f'[SERVER] ML marker recorded: event={event_id}, manifest_crash={manifest_crash_ms}')
    await ws.send_str(json.dumps({'type': 'ml_marker_ack', 'event_id': event_id, 'status': 'recorded', 'manifest_crash_ms': manifest_crash_ms, 'note': 'Marker recorded - ML detector continues independently'}))
    await broadcast(json.dumps({'type': 'ml_evaluation_marker', 'event_id': event_id, 'scenario_id': scenario_id, 'manifest_crash_ms': manifest_crash_ms}))

async def handle_ws(request):
    global current_df, playback_task, pi_client, current_meta, current_manifest, browser_ready
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    client_type = 'unknown'
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    log.warning('[SERVER] Malformed JSON - ignored')
                    continue
                mtype = data.get('type', '')
                if mtype == 'browser_connect':
                    client_type = 'browser'
                    browser_clients.add(ws)
                    log.info(f'[SERVER] Browser connected ({len(browser_clients)} total)')
                    await ws.send_str(json.dumps({'type': 'server_hello', 'pi_connected': pi_client is not None and not pi_client.closed, 'has_data': current_df is not None}))
                elif mtype == 'pi_connect':
                    client_type = 'pi'
                    pi_client   = ws
                    log.info('[SERVER] Raspberry Pi connected!')
                    await broadcast(json.dumps({'type': 'pi_status', 'connected': True}))
                elif mtype == 'start_playback':
                    import pandas as pd
                    browser_ready = False
                    csv_path = data.get('csv_path') or str(CSV_PATH)
                    if not os.path.exists(csv_path):
                        await ws.send_str(json.dumps({'type': 'error', 'msg': f'CSV not found: {csv_path}'}))
                        continue
                    try:
                        current_df = pd.read_csv(csv_path)
                    except Exception as e:
                        await ws.send_str(json.dumps({'type': 'error', 'msg': f'CSV read error: {e}'}))
                        continue
                    current_meta = {'biome': data.get('biome', 'hill_station'), 'scenario': data.get('scenario', ''), 'csv_path': csv_path}
                    current_manifest = {}
                    if MANIFEST_PATH.exists():
                        try:
                            with open(MANIFEST_PATH, 'r', encoding='utf-8') as mf:
                                current_manifest = json.load(mf)
                            log.info(f'[SERVER] Manifest loaded: {len(current_manifest.get(chr(115)+chr(101)+chr(103)+chr(109)+chr(101)+chr(110)+chr(116)+chr(115), []))} segments')
                        except Exception as e:
                            log.warning(f'[SERVER] Manifest load error: {e}')
                    dur = float(current_df['timestamp_ms'].max()) if 'timestamp_ms' in current_df.columns else len(current_df)
                    start_msg = json.dumps({'type': 'playback_start', 'rows': len(current_df), 'duration_ms': dur, 'manifest': current_manifest, **current_meta})
                    await broadcast(start_msg)
                    if pi_client and not pi_client.closed:
                        try:
                            await pi_client.send_str(start_msg)
                        except Exception:
                            pi_client = None
                    if playback_task and not playback_task.done():
                        playback_task.cancel()
                    playback_task = asyncio.create_task(playback_csv())
                    # NOTE: No subprocess launch here. Pi WS client is sole interactive ML owner.
                elif mtype == 'crash_test_requested':
                    if client_type == 'browser':
                        await handle_crash_test_requested(ws, data)
                elif mtype == 'ml_evaluation_marker':
                    if client_type == 'browser':
                        await handle_ml_evaluation_marker(ws, data)
                elif mtype == 'pi_verdict':
                    await broadcast(msg.data)
                elif mtype == 'actuator_status':
                    log.info(f'[SERVER] actuator_status from Pi: {data.get(chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))} event={data.get(chr(101)+chr(118)+chr(101)+chr(110)+chr(116)+chr(95)+chr(105)+chr(100))}')
                    await broadcast(msg.data)
                elif mtype == 'browser_ready':
                    browser_ready = True
                    log.info('[SERVER] Browser 3D world ready - streaming will begin')
                else:
                    log.debug(f'[SERVER] Unknown msg type from {client_type}: {repr(mtype)}')
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    except Exception as ex:
        log.error(f'[SERVER] WS error ({client_type}): {ex}')
    finally:
        browser_clients.discard(ws)
        if ws == pi_client:
            pi_client = None
            await broadcast(json.dumps({'type': 'pi_status', 'connected': False}))
            log.info('[SERVER] Pi disconnected')
        if client_type == 'browser':
            log.info(f'[SERVER] Browser disconnected ({len(browser_clients)} remain)')
    return ws

def build_app():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/index.html', handle_index)
    app.router.add_get('/dashboard', handle_dashboard)
    app.router.add_get('/dashboard.html', handle_dashboard)
    app.router.add_get('/api/manifest', handle_manifest)
    app.router.add_get('/{name}', handle_static)
    app.router.add_post('/api/generate', handle_generate)
    app.router.add_get('/ws', handle_ws)
    return app

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%H:%M:%S')
    print('=' * 58)
    print('  SMART AIRBAG HELMET - UNIFIED SERVER')
    print('=' * 58)
    print('  Open in browser ->  http://localhost:5500')
    print('  WebSocket       ->  ws://localhost:5500/ws')
    print('  Mode A: crash_test_requested  (forced demo)')
    print('  Mode B: ml_evaluation_marker  (ML evaluation)')
    print('=' * 58)
    app = build_app()
    web.run_app(app, host=['0.0.0.0', '::'], port=5500, access_log=None)
