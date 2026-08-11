# Termux Nyaa + Aria2 Torrent Manager (v3)
A lightning-fast, production-grade terminal torrent client designed specifically for **Android + Termux**.
This tool decouples the application layers correctly: **Python** handles the high-level interactive TUI, search caching, background prefetching, and network resiliency (retries/backoffs), while a persistent **aria2c daemon** natively manages the BitTorrent protocol, piece hashing, peer discovery, and integrity checks.
## Architecture
```text
┌────────────────────────────────────────────────────────┐
│                   Python TUI (Rich)                    │
│  - Search & Pagination     - Background Prefetch       │
│  - Thread-Safe Cache       - Retry & Exponential Backoff │
└──────────────────────────┬─────────────────────────────┘
                           │
                     XML-RPC (Secure Token)
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│                 aria2c Daemon (Background)             │
│  - BitTorrent Engine       - DHT / PEX / LPD           │
│  - Integrity Validation    - Session State Persistence │
└────────────────────────────────────────────────────────┘

```
## Features
 * **🚀 Asynchronous TUI Dashboard:** Built with rich, giving you a live-updating view of active downloads, transfer speeds, peer counts, and percentages without blocking input.
 * **🔄 Robust Network Layer:** Centralized HTTP requests featuring exponential backoff, jitter, timeout handling, and automatic recovery from transient errors (5xx, timeouts, 429 rate limits).
 * **🛡️ Cloudflare/Captcha Guard:** Validates HTML structure to prevent caching fake or blocked responses as empty search results.
 * **⚡ Parsed-Result Caching & Prefetching:** Caches fully parsed query data thread-safely and silently prefetches the next page in a background daemon thread for instant pagination.
 * **💾 Persistent Queue Sessions:** Automatically saves session state via aria2c so that active/incomplete downloads survive application restarts.
 * **🔒 Dynamic Security:** Automatically generates a cryptographically secure RPC token with restricted file permissions (0o600) on first boot.
## Prerequisites
Ensure you are running on Termux with storage permission configured:
```bash
# Setup storage access (if not already done)
termux-setup-storage

# Install Python and aria2
pkg update && pkg install python aria2

```
## Installation
 1. Clone or download this repository into your Termux environment.
 2. Install the required Python dependencies:
```bash
pip install requests beautifulsoup4 lxml rich

```
 3. Make the script executable:
```bash
chmod +x nyaa_manager.py

```
## Usage
Run the manager directly:
```bash
python3 nyaa_manager.py

```
### Controls & Navigation
 * **S** — Open the Nyaa search engine.
 * **P** — Pause a download (requires entering the partial GID displayed on the dashboard).
 * **R** — Resume a paused download.
 * **C** — Cancel/remove a download.
 * **Q** — Quit the TUI dashboard *(Note: The underlying aria2c daemon will continue running safely in the background).*
## Configuration
 * **Default Download Directory:** /storage/emulated/0/Download/Nyaa
 * **Session State File:** ~/.aria2.session
 * **RPC Token File:** ~/.nyaa_rpc_secret
## License
Distributed under the MIT License. Feel free to modify and adapt for your own offline workflow.
