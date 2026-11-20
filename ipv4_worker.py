"""Own a pinned Mihomo child and an IPv4-only bridge for one app session."""
import argparse
import ctypes
from ctypes import wintypes
import ipaddress
import shutil
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from ipv4_proxy import Server, check_exit, ExitMonitor, https_get, public_ipv4

ROOT = Path(__file__).resolve().parent


def log(message):
    print(datetime.now(timezone.utc).isoformat(timespec='seconds') + ' ' + message, flush=True)


class Job:
    """Windows closes the proxy child even if this supervisor is killed."""
    def __init__(self):
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.CreateJobObjectW.restype = wintypes.HANDLE
        self.k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        class Basic(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                        ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ['ro', 'wo', 'oo', 'rt', 'wt', 'ot']]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def parent_alive(pid):
    if not pid:
        return True
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == 258
    finally:
        k.CloseHandle(handle)


def pinned_block(policy):
    cfg = Path(os.environ['APPDATA']) / 'io.github.clash-verge-rev.clash-verge-rev' / 'clash-verge.yaml'
    text = cfg.read_text(encoding='utf-8')
    section = text.split('\nproxies:\n', 1)[1].split('\nproxy-groups:', 1)[0]
    blocks = re.split(r'(?m)(?=^- name:)', section)
    found = [b for b in blocks if b.splitlines() and b.splitlines()[0].removeprefix('- name:').strip() == policy['node']]
    if len(found) != 1:
        raise OSError('Selected node must be an inline Clash node.')
    block = found[0]
    if re.search(r'(?m)^  (dialer-proxy|interface-name):', block):
        raise OSError('Chained or interface-bound nodes are not supported.')
    endpoint = re.search(r'(?m)^  server: (.+)$', block)
    if not endpoint:
        raise OSError('Node endpoint is missing.')
    host = endpoint[1].strip().strip("'\"")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version != 4:
            raise OSError('IPv6 node endpoints are prohibited.')
        return block, 'dns:\n  enable: false\n'
    if not re.fullmatch(r'(?i)[a-z0-9-]+(?:\.[a-z0-9-]+)+', host):
        raise OSError('Invalid node hostname.')
    try:
        import yaml
    except ImportError:
        raise OSError('Domain nodes require PyYAML in the launcher Python.')
    live = yaml.safe_load(text)
    group = next((g for g in live.get('proxy-groups', []) if g['name']=='DNS-BOOT-IP'), {})
    members = group.get('proxies', [])
    boot = next((n for n in live['proxies'] if len(members)==1 and n['name']==members[0]), None)
    if not boot or boot.get('type') not in ('vless','vmess','trojan','ss') or any(k in boot for k in ('dialer-proxy','interface-name')):
        raise OSError('No supported independent DNS bootstrap node; no direct fallback.')
    public_ipv4(str(boot['server']))
    boot = dict(boot, name='AG-DNS-BOOT')
    if policy['node']==boot['name']:
        raise OSError('Bootstrap name conflict.')
    urls = live.get('dns', {}).get('proxy-server-nameserver', [])
    allowed = ['https://1.1.1.1/dns-query#DNS-BOOT-IP','https://8.8.8.8/dns-query#DNS-BOOT-IP']
    if not urls or any(u not in allowed for u in urls):
        raise OSError('Unsupported bootstrap DNS route; no direct fallback.')
    via = [u.replace('#DNS-BOOT-IP','#AG-DNS-BOOT') for u in urls]
    dns = {'enable':True, 'ipv6':False, 'nameserver':via, 'proxy-server-nameserver':via, 'fallback':[]}
    return block.rstrip()+'\n- '+json.dumps(boot,ensure_ascii=False), 'dns: '+json.dumps(dns)+'\n'



def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--stop-file', required=True)
    parser.add_argument('--parent-pid', type=int, default=0)
    parser.add_argument('--listen-port', type=int, default=0)
    parser.add_argument('--code-cmd')
    args = parser.parse_args()
    ready = Path(args.ready_file)
    stop = Path(args.stop_file)
    policy = json.loads((ROOT / 'ipv4-policy.json').read_text(encoding='utf-8'))
    runner = shutil.which('node')
    env = os.environ.copy()
    if not runner:
        cmd = args.code_cmd or shutil.which('code.cmd')
        if not cmd:
            raise OSError('Install Node.js or put the VS Code bin directory on PATH.')
        executable = Path(cmd).parent.parent / 'Code.exe'
        if not executable.is_file():
            raise OSError('Code.exe missing next to the detected VS Code bin directory.')
        runner = str(executable)
        env['ELECTRON_RUN_AS_NODE'] = '1'
    log('Node runtime: ' + runner)
    probe = subprocess.run([runner, str(ROOT / 'selected-node.cjs')], env=env,
                           capture_output=True, timeout=20)
    if probe.returncode:
        detail = probe.stderr.decode('utf-8', 'replace').strip()
        if 'no unique pipe' in detail or 'ENOENT' in detail:
            log('Clash controller unavailable: open Clash Verge and wait for its Mihomo core. SkyVPN alone is not supported.')
        elif detail in ('Use system proxy mode with TUN off.', 'Select a single proxy node.', 'Select a proxy in Clash first.', 'No identifiable default rule group.', 'Clash timeout', 'Clash controller unavailable'):
            log(detail)
        else:
            log('Failed to read the selected node from Clash Verge (controller or configuration error).')
        return 1
    selection = probe.stdout
    selected = json.loads(selection)['node']
    policy['node'] = selected
    # Locate the installed core next to the active GUI, including custom installation paths.
    gui = subprocess.check_output(['powershell.exe','-NoProfile','-Command',
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); (Get-Process clash-verge -ErrorAction Stop | Select-Object -First 1).Path"],
        timeout=10).decode('utf-8').strip()
    core = Path(gui).with_name('verge-mihomo.exe')
    if not core.is_file():
        raise OSError('Clash Verge core not found beside the running GUI.')
    policy['mihomo'] = str(core)
    block, dns_config = pinned_block(policy)
    runtime_root = ROOT / '.runtime'
    runtime_root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='node-', dir=runtime_root))
    assert folder.resolve().is_relative_to(runtime_root.resolve())
    backend_port = free_port()
    config = folder / 'config.yaml'
    process = None
    server = None
    job = None
    serving = False
    try:
        config.write_text(
            f'mixed-port: {backend_port}\n'
            'allow-lan: false\nbind-address: 127.0.0.1\nmode: rule\nlog-level: silent\n'
            'ipv6: false\n' + dns_config + 'tun:\n  enable: false\nsniffer:\n  enable: false\nproxies:\n'
            + block + '\nrules:\n  - ' + json.dumps('MATCH,' + policy['node'], ensure_ascii=False) + '\n',
            encoding='utf-8')
        job = Job()
        process = subprocess.Popen([policy['mihomo'], '-d', str(folder), '-f', str(config)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        for _ in range(100):
            if process.poll() is not None:
                raise OSError('Pinned proxy failed to start')
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', backend_port)) == 0:
                    break
            time.sleep(.1)
        else:
            raise OSError('Pinned proxy startup timeout')
        server = Server(('127.0.0.1', args.listen_port), backend_port)
        candidate = public_ipv4(https_get(server.resolver, 'api64.ipify.org').strip())
        observed = check_exit(server.resolver, candidate)
        if candidate != policy['expected_ipv4']:
            message = f"当前 Clash 节点：{selected}\n已核验 IPv4 出口：{candidate}\n上次认可的出口：{policy['expected_ipv4']}\n\n是否使用并记住新出口？"
            if ctypes.windll.user32.MessageBoxW(None, message, '确认新的代理出口', 0x124) != 6:
                raise OSError('New exit was not accepted.')
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ROOT, delete=False) as saved:
                json.dump({'expected_ipv4':candidate}, saved)
            os.replace(saved.name, ROOT / 'ipv4-policy.json')
        policy['expected_ipv4'] = candidate
        server.verified_at = time.monotonic()
        # Warm the affected endpoint, so application startup does not pay the first scan.
        server.resolver.addresses('daily-cloudcode-pa.googleapis.com', 443)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        serving = True
        status = {'port': server.server_address[1], 'expected_ipv4': policy['expected_ipv4'],
                  'observed': observed, 'backend_pid': process.pid, 'worker_pid': os.getpid(), 'state': 'active'}
        ready.write_text(json.dumps(status), encoding='utf-8')
        log('READY IPv4 exit verified: ' + policy['expected_ipv4'])
        monitor = ExitMonitor(server, policy['expected_ipv4'])
        while not stop.exists() and parent_alive(args.parent_pid):
            if process.poll() is not None:
                raise OSError('Pinned proxy stopped; no fallback')
            report = monitor.poll()
            if report:
                status['state'] = report['state']
                status['last_check'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                status['last_error'] = report.get('reason')
                ready.write_text(json.dumps(status), encoding='utf-8')
                if report['state'] == 'paused':
                    log('PAUSED new connections; retry in ' + str(report['retry_seconds']) + 's: ' + report['reason'])
                else:
                    log(('RECOVERED' if report['recovered'] else 'RECHECK') + ' IPv4 exit verified: ' + policy['expected_ipv4'])
            time.sleep(.5)
        return 0
    except Exception as exc:
        # Exception messages do not include the node configuration or credentials.
        log('STOPPED: ' + str(exc))
        return 1
    finally:
        if server:
            server.close_tunnels()
            if serving:
                server.shutdown()
            server.server_close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if job:
            job.close()
        ready.unlink(missing_ok=True)
        for file in folder.iterdir():
            if file.is_file():
                file.unlink()
        folder.rmdir()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, OSError) else type(exc).__name__
        log("STOPPED before startup: " + detail)
        raise SystemExit(1)
