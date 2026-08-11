#!/usr/bin/env python3

import os
import sys
import time
import json
import random
import shutil
import subprocess
import threading
import xmlrpc.client
import select
import tty
import termios
import requests
from urllib.parse import quote, urljoin

# --- Optional/Graceful Imports ---
try:
    from bs4 import BeautifulSoup
    BeautifulSoup("<html></html>", "lxml")
    PARSER = "lxml"
except Exception:
    from bs4 import BeautifulSoup
    PARSER = "html.parser"

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print("This version requires 'rich' for the TUI.")
    print("Please run: pip install rich")
    sys.exit(1)

# --- Configuration ---
BASE_URL = "https://nyaa.si"
DEFAULT_DOWNLOAD_DIR = "/storage/emulated/0/Download/Nyaa"
HISTORY_FILE = os.path.expanduser("~/.nyaa_history.json")
SECRET_FILE = os.path.expanduser("~/.nyaa_rpc_secret")

# Aria2c Daemon Config
RPC_URL = "http://localhost:6800/rpc"
ARIA2_SESSION = os.path.expanduser("~/.aria2.session")

def get_or_create_rpc_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r") as f:
            return f.read().strip()
    
    import secrets
    new_secret = secrets.token_hex(16)
    with open(SECRET_FILE, "w") as f:
        f.write(new_secret)
    # Secure the file so only the current user can read/write it
    os.chmod(SECRET_FILE, 0o600)
    return new_secret

RPC_SECRET = get_or_create_rpc_secret()

console = Console()

# --- Thread-Safe State & Caching ---
_RESULT_CACHE = {}
_FETCH_EVENTS = {}
_CACHE_LOCK = threading.Lock()

def create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Android; Termux) AppleWebKit/537.36",
        "Accept-Encoding": "gzip, deflate, br"
    })
    return s

# --- History Management ---
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_history(title, magnet):
    history = load_history()
    if not any(entry['magnet'] == magnet for entry in history):
        history.append({"title": title, "magnet": magnet})
        history = history[-50:]
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f)

# --- Aria2 XML-RPC Wrapper ---
class Aria2RPCClient:
    """Wraps xmlrpc to automatically inject the token:secret into aria2 RPC calls."""
    def __init__(self, url, secret):
        self.server = xmlrpc.client.ServerProxy(url)
        self.token = f"token:{secret}"
        
    def getVersion(self): 
        return self.server.aria2.getVersion(self.token)
        
    def tellActive(self): 
        return self.server.aria2.tellActive(self.token)
        
    def tellWaiting(self, offset, num): 
        return self.server.aria2.tellWaiting(self.token, offset, num)
        
    def tellStopped(self, offset, num): 
        return self.server.aria2.tellStopped(self.token, offset, num)
        
    def addUri(self, uris, options=None): 
        if options:
            return self.server.aria2.addUri(self.token, uris, options)
        return self.server.aria2.addUri(self.token, uris)
        
    def pause(self, gid): 
        return self.server.aria2.pause(self.token, gid)
        
    def unpause(self, gid): 
        return self.server.aria2.unpause(self.token, gid)
        
    def remove(self, gid): 
        return self.server.aria2.remove(self.token, gid)
        
    def saveSession(self):
        """Forces aria2 to immediately persist the queue to the .aria2.session file"""
        return self.server.aria2.saveSession(self.token)

# --- Network & Retry Layer ---
def fetch_html_with_retry(url, http_session, is_background=False, max_attempts=3):
    """
    Robust HTTP helper. Handles timeouts, connection errors, 5xx, and 429 backoffs.
    Does NOT retry permanent 4xx errors. Applies jittered exponential backoff.
    """
    for attempt in range(max_attempts):
        try:
            r = http_session.get(url, timeout=(5.0, 15.0))
            
            if r.status_code == 200:
                return r.text
            
            elif r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2.0
                delay = min(30.0, delay)
                if not is_background:
                    console.print("[yellow][!] Nyaa returned HTTP 429 (Too Many Requests)[/yellow]")
            
            elif r.status_code >= 500:
                delay = min(30.0, (2 ** attempt)) + random.uniform(0.1, 0.5)
                if not is_background:
                    console.print(f"[yellow][!] Nyaa returned HTTP {r.status_code}[/yellow]")
            
            else:
                r.raise_for_status()

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            delay = min(30.0, (2 ** attempt)) + random.uniform(0.1, 0.5)
            if not is_background:
                err_type = "Timeout" if isinstance(e, requests.exceptions.Timeout) else "Connection Error"
                console.print(f"[yellow][!] Network {err_type}[/yellow]")
                
        except requests.exceptions.RequestException as e:
            if not is_background:
                console.print(f"[bold red][!] Fatal HTTP Error: {e}[/bold red]")
            return None

        # Handle backoff delay
        if attempt < max_attempts - 1:
            if not is_background:
                console.print(f"[cyan][↻] Retry {attempt + 1}/{max_attempts - 1} in {delay:.1f}s...[/cyan]")
            time.sleep(delay)
        else:
            if not is_background:
                console.print(f"[bold red][x] Request failed after {max_attempts} attempts.[/bold red]")
            return None

# --- Nyaa Fetching Engine ---
def fetch_and_parse(query, page, sort, is_background=False):
    """
    Thread-safe fetcher. Detects Cloudflare/Captchas gracefully.
    Never caches temporary network or parsing failures.
    """
    cache_key = (query, page, sort)
    
    # Ownership Loop: If a background thread fails while we wait, we loop back
    # and claim ownership to try again ourselves.
    while True:
        with _CACHE_LOCK:
            if cache_key in _RESULT_CACHE:
                return _RESULT_CACHE[cache_key]
                
            if cache_key in _FETCH_EVENTS:
                event = _FETCH_EVENTS[cache_key]
                needs_fetch = False
            else:
                event = threading.Event()
                _FETCH_EVENTS[cache_key] = event
                needs_fetch = True

        if needs_fetch:
            break # We claimed ownership, proceed to fetch
            
        if not is_background:
            event.wait() # Wait for the owner. Loop repeats to check if they succeeded.
        else:
            return None # We're a background thread and someone else is already fetching. Yield.

    # --- We are now the active fetching thread ---
    url = f"{BASE_URL}/?f=0&c=0_0&q={quote(query)}&p={page}&s={sort}&o=desc"
    http = create_session()
    results = None
    
    try:
        # Allow exactly 1 HTML re-fetch if parsing detects a captcha/anomaly
        for parse_attempt in range(2):
            html = fetch_html_with_retry(url, http, is_background)
            if not html:
                break

            try:
                soup = BeautifulSoup(html, PARSER)
                table = soup.select_one("table.torrent-list")
                
                if table is None:
                    # Cloudflare/Captcha validation: Ensure it's a real Nyaa page
                    if not soup.find("nav", class_="navbar"):
                        raise ValueError("Unexpected HTML content (Cloudflare/Captcha blocking?)")
                    # Legitimately 0 search results
                    results = []
                    break
                
                results = []
                for row in table.select("tbody tr"):
                    cols = row.select("td")
                    if len(cols) < 7:
                        continue

                    title_links = row.select("td:nth-of-type(2) a:not(.comments)")
                    if not title_links:
                        continue

                    title_link = title_links[0]
                    title = title_link.get("title") or title_link.get_text(strip=True)
                    magnet_tag = row.select_one('a[href^="magnet:"]')
                    
                    if magnet_tag:
                        values = [c.get_text(" ", strip=True) for c in cols]
                        results.append({
                            "title": title,
                            "magnet": magnet_tag["href"],
                            "size": values[3] if len(values) > 3 else "?",
                            "seeders": values[5] if len(values) > 5 else "?",
                            "leechers": values[6] if len(values) > 6 else "?"
                        })
                break
                
            except Exception as e:
                if not is_background:
                    console.print(f"[yellow][!] HTML validation failed (Attempt {parse_attempt + 1}/2): {e}[/yellow]")
                if parse_attempt == 0 and not is_background:
                    console.print("[cyan][↻] Requesting fresh HTML...[/cyan]")
                results = None
    finally:
        http.close()

    # Update cache securely. Only save successful pulls.
    with _CACHE_LOCK:
        if results is not None:
            _RESULT_CACHE[cache_key] = results
            if not is_background:
                console.print("[green][✓] Search successful[/green]")
        
        event.set()
        if cache_key in _FETCH_EVENTS:
            del _FETCH_EVENTS[cache_key]
        
    return results

def prefetch_page(query, page, sort):
    fetch_and_parse(query, page, sort, is_background=True)

# --- Aria2 RPC Management ---
def format_bytes(size):
    try:
        size = float(size)
    except ValueError:
        return "0 B"
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"

def ensure_aria2_running():
    # 1. Initialize custom RPC client
    server = Aria2RPCClient(RPC_URL, RPC_SECRET)
    
    # 2. Check if already running and responding
    try:
        server.getVersion()
        return server
    except Exception:
        pass

    # 3. Verify installation
    if not shutil.which("aria2c"):
        raise RuntimeError("aria2c is not installed. Run: pkg install aria2")

    # 4. Prepare Session File to prevent aria2 from crashing on missing --input-file
    if not os.path.exists(ARIA2_SESSION):
        open(ARIA2_SESSION, 'a').close()

    console.print("[yellow]Starting persistent aria2c daemon...[/yellow]")
    
    # 5. Launch detached process (start_new_session completely insulates it from Python's Ctrl+C)
    # Note: Android Force Stop will still kill this, hence explicit session saving later.
    proc = subprocess.Popen([
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        "--rpc-listen-port=6800",
        f"--rpc-secret={RPC_SECRET}",
        "--dir", DEFAULT_DOWNLOAD_DIR,
        "--save-session", ARIA2_SESSION,
        "--save-session-interval=30",
        "--input-file", ARIA2_SESSION,
        "--file-allocation=none",
        "--disk-cache=16M",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        "--bt-max-peers=80",
        "--check-integrity=true",
        "--connect-timeout=10",
        "--timeout=20",
        "--bt-tracker-connect-timeout=10",
        "--seed-time=0"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    
    # 6. Poll strictly via RPC for readiness
    for _ in range(10):
        time.sleep(0.5)
        # We only check proc.poll() in case it crashed immediately despite detachment
        if proc.poll() is not None:
            raise RuntimeError(f"aria2c daemon crashed instantly (Exit code {proc.returncode}).")
        
        try:
            server.getVersion()
            return server
        except Exception:
            continue
            
    raise RuntimeError("aria2c detached, but the RPC interface did not respond within 5 seconds.")

# --- Input Handling (Termux/Unix Non-Blocking) ---
def get_keypress(timeout=1.0):
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            key = sys.stdin.read(1)
            if key == '\x03': 
                raise KeyboardInterrupt
            return key.lower()
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def standard_input(prompt_text):
    console.print(prompt_text, end="")
    return input()

# --- TUI Views ---
def draw_dashboard(server):
    console.clear()
    console.print(Panel(Text("Nyaa + Aria2 Manager", justify="center", style="bold cyan")))
    
    try:
        active = server.tellActive()
        waiting = server.tellWaiting(0, 10)
        stopped = server.tellStopped(0, 10)
        all_downloads = active + waiting + stopped
    except Exception as e:
        console.print(f"[red]Lost connection to aria2c daemon: {e}[/red]")
        return

    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("GID", style="dim", width=17)
    table.add_column("Name", min_width=30)
    table.add_column("Status", width=10)
    table.add_column("Progress", justify="right")
    table.add_column("Speed", justify="right")
    table.add_column("Peers", justify="right")
    
    if not all_downloads:
        table.add_row("", "No active or recent downloads.", "", "", "", "")
    else:
        for d in all_downloads:
            gid = d.get('gid', '')
            status = d.get('status', 'unknown')
            
            name = "Fetching Metadata..."
            if 'bittorrent' in d and 'info' in d['bittorrent']:
                name = d['bittorrent']['info'].get('name', name)

            total = float(d.get('totalLength', 0))
            completed = float(d.get('completedLength', 0))
            speed = float(d.get('downloadSpeed', 0))
            peers = d.get('connections', '0')
            
            percent = (completed / total * 100) if total > 0 else 0
            prog_str = f"[green]{percent:.1f}%[/green]" if status == "active" else f"{percent:.1f}%"
            
            status_colors = {
                "active": "[bold green]Active[/]",
                "waiting": "[bold yellow]Waiting[/]",
                "paused": "[bold yellow]Paused[/]",
                "error": "[bold red]Error[/]",
                "complete": "[bold blue]Done[/]",
            }
            
            speed_str = format_bytes(speed) + "/s" if status == "active" else "-"
            
            table.add_row(
                gid[:8] + "...", 
                name[:45] + ("..." if len(name)>45 else ""),
                status_colors.get(status, status), 
                prog_str, 
                speed_str, 
                peers
            )

    console.print(table)
    console.print("\n[bold blue][S][/] Search Nyaa  |  [bold blue][P][/] Pause  |  [bold blue][R][/] Resume  |  [bold blue][C][/] Cancel  |  [bold blue][Q][/] Quit")

def run_search_ui(server, default_dir):
    console.clear()
    query = standard_input("[bold cyan]Search Nyaa:[/bold cyan] ").strip()
    if not query:
        return

    page = 1
    sort_mode = "seeders"
    
    while True:
        console.clear()
        console.print(f"[bold blue]Searching Nyaa:[/] {query} | Page {page} | Sort: {sort_mode}\n")
        
        results = fetch_and_parse(query, page, sort_mode, is_background=False)
        
        if results is not None:
            threading.Thread(target=prefetch_page, args=(query, page + 1, sort_mode), daemon=True).start()

            if results:
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("#", style="dim", width=3)
                table.add_column("Title")
                table.add_column("Size", justify="right", style="yellow")
                table.add_column("Seed", justify="right", style="green")
                table.add_column("Leech", justify="right", style="red")

                for i, r in enumerate(results, 1):
                    table.add_row(str(i), r['title'], r['size'], r['seeders'], r['leechers'])
                
                console.print(table)
            else:
                console.print("[yellow]No results found.[/yellow]")
        else:
            console.print("\n[bold red]Cannot display results due to network/parsing errors.[/bold red]")

        console.print("\n[bold]Select number[/] to download | [bold blue][N][/]ext | [bold blue][P][/]rev | [bold blue][S][/]ort | [bold blue][B][/]ack to Dash")
        
        choice = standard_input("[bold magenta]Action:[/bold magenta] ").strip().lower()

        if choice == 'b' or choice == 'q':
            break
        elif choice == 'n':
            page += 1
            continue
        elif choice == 'p':
            page = max(1, page - 1)
            continue
        elif choice == 's':
            sort_modes = {"seeders": "size", "size": "id", "id": "seeders"}
            sort_mode = sort_modes.get(sort_mode, "seeders")
            page = 1
            continue

        try:
            number = int(choice)
            if results and 1 <= number <= len(results):
                selected = results[number - 1]
                
                try:
                    os.makedirs(default_dir, exist_ok=True)
                    if not os.access(default_dir, os.W_OK):
                        raise PermissionError("Directory is not writable.")
                except Exception as e:
                    console.print(f"\n[bold red][x] Storage Error:[/bold red] Cannot write to '{default_dir}'")
                    console.print(f"Reason: {e}")
                    console.print("Please check Termux storage permissions (`termux-setup-storage`).")
                    time.sleep(3)
                    continue

                server.addUri([selected['magnet']], {"dir": default_dir})
                save_history(selected['title'], selected['magnet'])
                console.print(f"[green][✓] Added to download queue:[/] {selected['title']}")
                time.sleep(1)
                break
        except ValueError:
            pass

# --- Main Application Loop ---
def main():
    try:
        server = ensure_aria2_running()
    except Exception as e:
        console.print(f"\n[bold red]Fatal Startup Error:[/bold red]\n{e}")
        return

    try:
        while True:
            draw_dashboard(server)
            
            key = get_keypress(timeout=1.0)
            
            if key is None:
                continue
                
            if key == 'q':
                break
            elif key == 's':
                run_search_ui(server, DEFAULT_DOWNLOAD_DIR)
            elif key in ('p', 'r', 'c'):
                console.print("\n")
                gid_prefix = standard_input("[bold magenta]Enter partial GID:[/bold magenta] ").strip()
                if gid_prefix:
                    try:
                        downloads = server.tellActive() + server.tellWaiting(0, 10)
                        target_gid = next((d['gid'] for d in downloads if d['gid'].startswith(gid_prefix)), None)
                        
                        if target_gid:
                            if key == 'p': server.pause(target_gid)
                            elif key == 'r': server.unpause(target_gid)
                            elif key == 'c': server.remove(target_gid)
                    except Exception:
                        pass 
    finally:
        # Guarantee queue metadata is safely written on UI exit,
        # guarding against sudden Termux OS kills post-exit.
        try:
            server.saveSession()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        main()
        console.clear()
        console.print("[bold green]Dashboard closed. aria2c daemon is still running safely in the background.[/bold green]")
        console.print("[dim]aria2c download session gracefully saved.[/dim]")
    except KeyboardInterrupt:
        console.clear()
        console.print("\n[bold green]Exited gracefully. Background downloads remain active.[/bold green]")
        console.print("[dim]aria2c download session gracefully saved.[/dim]")



