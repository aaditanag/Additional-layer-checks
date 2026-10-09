import asyncio, json, os, sys, time, argparse, math, warnings
warnings.filterwarnings('ignore')

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    from l3.detector            import Layer3Detector
    from l4.actuator            import ActuatorEngine
    from l5.post_crash_analysis import run_post_crash_analysis
except ImportError as e:
    print(f'[PI CLIENT] Missing module: {e}')
    print('[PI CLIENT] Make sure l3/, l4/, l5/ folders exist inside layer_pi/')
    sys.exit(1)

LABEL_MAP = {0: 'Normal', 1: 'Near-Crash', 2: 'CRASH'}

# Forced demo: allowed scenario IDs and deduplication
ALLOWED_CRASH_TEST_SCENARIOS = {'front_collision_demo_01', 'existing'}
_seen_test_event_ids = {}
DEDUP_TTL_S = 300

def _is_duplicate_test_event(event_id):
    now = time.monotonic()
    expired = [k for k, t in _seen_test_event_ids.items() if now - t > DEDUP_TTL_S]
    for k in expired:
        del _seen_test_event_ids[k]
    if event_id in _seen_test_event_ids:
        return True
    _seen_test_event_ids[event_id] = now
    return False


async def handle_crash_test(ws, data, actuator, hardware):
    """
    Handle a Mode A forced crash test event from the server.
    Issues a predefined buzzer/LED warning pattern ONLY.
    Airbag remains disarmed. Never directly controlled by browser messages.
    """
    event_id    = data.get('event_id', '')
    scenario_id = data.get('scenario_id', '')
    mode        = data.get('mode', '')

    # Validate
    if not event_id or len(event_id) > 128:
        print(f'[PI CLIENT] crash_test rejected: invalid event_id')
        await ws.send(json.dumps({'type': 'actuator_status', 'event_id': event_id, 'status': 'rejected', 'reason': 'invalid event_id', 'airbag_armed': False}))
        return

    if scenario_id not in ALLOWED_CRASH_TEST_SCENARIOS:
        print(f'[PI CLIENT] crash_test rejected: unsupported scenario_id {repr(scenario_id)}')
        await ws.send(json.dumps({'type': 'actuator_status', 'event_id': event_id, 'status': 'rejected', 'reason': f'unsupported scenario_id: {repr(scenario_id)}', 'airbag_armed': False}))
        return

    if mode != 'forced':
        print(f'[PI CLIENT] crash_test rejected: unexpected mode {repr(mode)}')
        await ws.send(json.dumps({'type': 'actuator_status', 'event_id': event_id, 'status': 'rejected', 'reason': f'unexpected mode: {repr(mode)}', 'airbag_armed': False}))
        return

    if _is_duplicate_test_event(event_id):
        print(f'[PI CLIENT] crash_test rejected: duplicate event_id {event_id}')
        await ws.send(json.dumps({'type': 'actuator_status', 'event_id': event_id, 'status': 'rejected', 'reason': 'duplicate event_id', 'airbag_armed': False}))
        return

    print(f'[PI CLIENT] *** FORCED DEMO CRASH TEST: event={event_id}, scenario={scenario_id} ***')

    # Issue predefined warning pattern ONLY (no airbag)
    warning_ok  = False
    warning_err = None
    try:
        # 3 rapid pulses - predefined demo pattern
        actuator.pulse_warning(n_pulses=3, pulse_ms=80)
        warning_ok = True
        print(f'[PI CLIENT] Buzzer/LED warning pattern issued (3 x 80ms pulses)')
    except Exception as e:
        warning_err = str(e)
        print(f'[PI CLIENT] Warning output error: {e}')

    # Report result — clearly distinct from ML verdict
    ack = {
        'type'        : 'actuator_status',
        'event_id'    : event_id,
        'scenario_id' : scenario_id,
        'mode'        : 'forced',
        'status'      : 'buzzer_command_issued' if warning_ok else 'actuator_error',
        'hardware'    : hardware,
        'airbag_armed': False,   # ALWAYS false — airbag disarmed during demo
        'error'       : warning_err,
        'note'        : 'Forced demo test — NOT an ML crash prediction. Airbag remains disarmed.',
    }
    await ws.send(json.dumps(ack))
    print(f'[PI CLIENT] actuator_status sent: {ack[chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115)]}')


async def run_ws_client(server_url, hardware, phone):
    try:
        import websockets
    except ImportError:
        print('[PI CLIENT] websockets not installed. Run: pip install websockets')
        sys.exit(1)

    detector    = Layer3Detector()
    actuator    = ActuatorEngine(hardware=hardware, emergency_number=phone)
    deploy_data = None

    print('=' * 60)
    print('  SMART AIRBAG HELMET - RASPBERRY PI WebSocket CLIENT')
    print(f'  Connecting to : {server_url}')
    print(f'  Hardware GPIO : {"YES" if hardware else "SIMULATION"}')
    print('=' * 60)

    async with websockets.connect(server_url) as ws:
        print('[PI CLIENT] Connected to server.')
        await ws.send(json.dumps({'type': 'pi_connect'}))

        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get('type', '')

            # New scenario starting
            if mtype == 'playback_start':
                print(f'[PI CLIENT] Scenario starting: {msg.get(chr(115)+chr(99)+chr(101)+chr(110)+chr(97)+chr(114)+chr(105)+chr(111),"?")} | Biome: {msg.get(chr(98)+chr(105)+chr(111)+chr(109)+chr(101),"?")}')
                detector    = Layer3Detector()
                actuator    = ActuatorEngine(hardware=hardware, emergency_number=phone)
                deploy_data = None

            # Mode A: Forced crash test event (from server, validated)
            elif mtype == 'crash_test_requested':
                await handle_crash_test(ws, msg, actuator, hardware)

            # Process sensor frame — ML detector runs independently
            elif mtype == 'sensor_frame' and not actuator.deployed:
                sample = {
                    'ax': msg['ax'], 'ay': msg['ay'], 'az': msg['az'],
                    'gx': msg['gx'], 'gy': msg['gy'], 'gz': msg['gz'],
                    'hg_ax': msg['hg_ax'], 'hg_ay': msg['hg_ay'], 'hg_az': msg['hg_az'],
                }
                t_ms  = msg.get('t_ms', 0)
                t_s   = t_ms / 1000.0

                t0     = time.perf_counter()
                result = detector.process_sample(sample, current_time_s=t_s)
                lat_ms = (time.perf_counter() - t0) * 1000

                # Near-crash warning (from real ML, not from forced test)
                if result['p1_label'] == 1:
                    actuator.pulse_warning(n_pulses=1, pulse_ms=50)

                p1_lbl = result.get('p1_label') or 0
                p2_lbl = result.get('p2_label') if result.get('p2_label') is not None else 0
                p1_cr  = result.get('p1_crash', result.get('p1_crash_prob', 0.0)) or 0.0
                p2_cr  = result.get('p2_crash', result.get('p2_crash_prob', 0.0)) or 0.0

                verdict = {
                    'type'      : 'pi_verdict',
                    't_ms'      : t_ms,
                    'p1_label'  : int(p1_lbl),
                    'p1_crash'  : float(p1_cr),
                    'p2_label'  : int(p2_lbl),
                    'p2_crash'  : float(p2_cr),
                    'det_score' : float(result.get('det_score') or 0.0),
                    'gate_count': int(result.get('gate_count') or 0),
                    'latency_ms': round(lat_ms, 2),
                    'deployed'  : False,
                    'source'    : 'ml_detector',  # always from real ML, not forced test
                }
                await ws.send(json.dumps(verdict))

                # ML-confirmed crash -> deploy
                if result['new_deploy']:
                    fired = actuator.deploy_airbag(reason=result.get('decision_reason', 'ML_CRASH'))
                    if fired:
                        print(f'\n[PI CLIENT] *** AIRBAG DEPLOYED (ML) at t={t_ms}ms ***')
                        actuator.send_emergency_sms()
                        bb = list(detector.blackbox)
                        peak_g = max((math.sqrt(e.get('hg_ax',0)**2+e.get('hg_ay',0)**2+e.get('hg_az',0)**2)/9.81 for e in bb), default=0)
                        peak_gyro = max((math.sqrt(e.get('gx',0)**2+e.get('gy',0)**2+e.get('gz',0)**2) for e in bb), default=0)
                        deploy_data = {'t_ms': t_ms, 'reason': result.get('decision_reason','ML_CRASH'), 'blackbox': bb, 'peak_g': round(peak_g,2), 'peak_gyro': round(peak_gyro,1)}
                        verdict['deployed']  = True
                        verdict['deploy_ts'] = t_ms
                        verdict['peak_g']    = deploy_data['peak_g']
                        verdict['peak_gyro'] = deploy_data['peak_gyro']
                        await ws.send(json.dumps(verdict))
                        try:
                            metrics = run_post_crash_analysis(blackbox_entries=bb, deploy_ts_ms=t_ms, crash_onset_ms=t_ms, latency_ms=lat_ms, reason=deploy_data['reason'])
                            await ws.send(json.dumps({'type': 'pi_verdict', 't_ms': t_ms, 'deployed': True, 'severity': metrics.get('severity','SEVERE'), 'peak_g': metrics.get('peak_accel_hg_g',deploy_data['peak_g']), 'peak_gyro': metrics.get('peak_gyro_deg_per_s',deploy_data['peak_gyro']), 'latency_ms': lat_ms, 'deploy_ts': t_ms, 'gate_count': int(result['gate_count']), 'p1_label': int(result['p1_label']), 'p2_label': int(result['p2_label']), 'det_score': float(result['det_score']), 'source': 'ml_detector'}))
                        except Exception as e:
                            print(f'[PI CLIENT] Layer 5 error: {e}')

            elif mtype == 'playback_done':
                print('[PI CLIENT] Scenario complete.')
                if not actuator.deployed:
                    print('[PI CLIENT] No crash detected - safe ride.')

        actuator.cleanup()


def run_standalone(csv_path, hardware, phone):
    import pandas as pd
    from l3.detector import Layer3Detector

    print('=' * 65)
    print('  RASPBERRY PI STANDALONE MODE (Offline ML Execution)')
    print(f'  Sensor CSV : {csv_path}')
    print(f'  Hardware   : {"REAL GPIO" if hardware else "SIMULATION"}')
    print('=' * 65)

    if not os.path.exists(csv_path):
        print(f'\n[ERROR] CSV file not found: {csv_path}')
        return

    lock_file = os.path.join(ROOT, 'AIRBAG_DEPLOYED.lock')
    if os.path.exists(lock_file):
        os.remove(lock_file)
        print('  [SAFETY] Cleared stale AIRBAG_DEPLOYED.lock')

    detector   = Layer3Detector()
    actuator   = ActuatorEngine(hardware=hardware, emergency_number=phone)
    df         = pd.read_csv(csv_path)
    total_rows = len(df)
    print(f'  Loaded {total_rows:,} sensor samples (~{total_rows/1000:.1f}s of data)\n')

    first_crash = None
    last_print  = 0
    deployed    = False

    for idx, row in df.iterrows():
        sample = {
            'ax': float(row['ax']), 'ay': float(row['ay']), 'az': float(row['az']),
            'gx': float(row['gx']), 'gy': float(row['gy']), 'gz': float(row['gz']),
            'hg_ax': float(row['hg_ax']), 'hg_ay': float(row['hg_ay']), 'hg_az': float(row['hg_az']),
        }
        t_ms  = float(row.get('timestamp_ms', idx))
        label = int(row.get('label', 0))

        if label == 2 and first_crash is None:
            first_crash = t_ms

        t0     = time.perf_counter()
        result = detector.process_sample(sample, current_time_s=t_ms/1000.0)
        lat_ms = (time.perf_counter() - t0) * 1000

        g_force  = math.sqrt(sample['ax']**2 + sample['ay']**2 + sample['az']**2) / 9.81
        lean_deg = abs(sample['ay'] * 4.5)

        if idx - last_print >= 2000:
            last_print = idx
            pct  = (idx / total_rows) * 100
            p1n  = 'CRASH' if result['p1_label'] == 2 else 'NEAR' if result['p1_label'] == 1 else 'NORMAL'
            print(f'  [{pct:5.1f}%] t={t_ms/1000:6.1f}s | G: {g_force:4.1f}g | Lean: {lean_deg:4.1f} | {p1n} ({lat_ms:.2f}ms)')

        if result['p1_label'] in (1, 2) and result.get('det_score', 0) >= 0.15:
            actuator.pulse_warning(n_pulses=1, pulse_ms=50)

        if result['new_deploy'] and not actuator.deployed:
            deployed = True
            fired    = actuator.deploy_airbag(reason=result.get('decision_reason', 'ML_CRASH'))
            actuator.send_emergency_sms()
            det_lat  = (t_ms - first_crash) if first_crash else 0
            bb       = list(detector.blackbox)
            peak_g   = max((math.sqrt(e.get('hg_ax',0)**2+e.get('hg_ay',0)**2+e.get('hg_az',0)**2)/9.81 for e in bb), default=g_force)
            peak_gyro = max((math.sqrt(e.get('gx',0)**2+e.get('gy',0)**2+e.get('gz',0)**2) for e in bb), default=0)
            print('=' * 65)
            print('  CRASH DETECTED - AIRBAG DEPLOYED!')
            print(f'  Deploy Timestamp : {t_ms:.1f} ms ({t_ms/1000:.2f}s)')
            print(f'  Detection Latency: {det_lat:.1f} ms')
            print(f'  Gate             : {result.get(chr(103)+chr(97)+chr(116)+chr(101)+chr(95)+chr(99)+chr(111)+chr(117)+chr(110)+chr(116),3)}/3 (Confirmed)')
            print(f'  Peak Impact G    : {peak_g:.1f} g')
            print(f'  Peak Gyro        : {peak_gyro:.1f} deg/s')
            print('=' * 65)
            run_post_crash_analysis(bb, t_ms, first_crash, det_lat, result.get('decision_reason','ML_CRASH'))
            break

    if not deployed:
        print('=' * 65)
        print('  RIDE COMPLETED SAFELY - NO CRASH DETECTED')
        print(f'  Processed {total_rows:,} samples successfully.')
        print('=' * 65)

    actuator.cleanup()


def main():
    p = argparse.ArgumentParser(description='Raspberry Pi Layer Pi Client')
    p.add_argument('--server',     default='ws://localhost:5500/ws')
    p.add_argument('--hardware',   action='store_true')
    p.add_argument('--phone',      default='+91XXXXXXXXXX')
    p.add_argument('--standalone', action='store_true')
    p.add_argument('--csv',        default='../data/pre_decided_sensor_data.csv')
    args = p.parse_args()

    if args.standalone:
        run_standalone(args.csv, args.hardware, args.phone)
    else:
        asyncio.run(run_ws_client(args.server, args.hardware, args.phone))


if __name__ == '__main__':
    main()
