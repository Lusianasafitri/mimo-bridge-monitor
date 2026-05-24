#!/usr/bin/env python3
"""MiMo Bridge Monitor - Cross-chain bridge status and transaction tracker."""

import json
import time
import logging
import sqlite3
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bridge-monitor")

SUPPORTED_BRIDGES = {
    "stargate": {"name": "Stargate", "chains": ["ethereum", "arbitrum", "optimism", "polygon", "bsc"], "contracts": {}},
    "hop": {"name": "Hop Protocol", "chains": ["ethereum", "arbitrum", "optimism", "polygon"], "contracts": {}},
    "across": {"name": "Across Protocol", "chains": ["ethereum", "arbitrum", "optimism"], "contracts": {}},
    "synapse": {"name": "Synapse", "chains": ["ethereum", "arbitrum", "optimism", "polygon", "bsc"], "contracts": {}},
    "wormhole": {"name": "Wormhole", "chains": ["ethereum", "solana", "bsc", "polygon"], "contracts": {}},
}


@dataclass
class BridgeTransaction:
    bridge: str
    source_chain: str
    dest_chain: str
    token: str
    amount: float
    status: str
    tx_hash: str
    timestamp: int
    completion_time: Optional[int] = None
    gas_used: int = 0
    fee: float = 0.0

    @property
    def duration(self) -> Optional[int]:
        if self.completion_time:
            return self.completion_time - self.timestamp
        return None

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


@dataclass
class ChainStatus:
    chain: str
    bridge: str
    status: str
    latency: int
    last_check: int
    error_count: int = 0


class BridgeMonitor:
    def __init__(self, db_path: str = "bridge_monitor.db"):
        self.transactions: List[BridgeTransaction] = []
        self.chain_status: Dict[str, Dict[str, ChainStatus]] = {}
        self.alerts: List[Dict] = []
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bridge TEXT, source_chain TEXT, dest_chain TEXT,
                token TEXT, amount REAL, status TEXT, tx_hash TEXT UNIQUE,
                timestamp INTEGER, completion_time INTEGER,
                gas_used INTEGER DEFAULT 0, fee REAL DEFAULT 0
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bridge ON transactions(bridge)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON transactions(status)")
            conn.execute("""CREATE TABLE IF NOT EXISTS chain_status (
                chain TEXT, bridge TEXT, status TEXT, latency INTEGER,
                last_check INTEGER, error_count INTEGER DEFAULT 0,
                PRIMARY KEY (chain, bridge)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                severity TEXT, bridge TEXT, chain TEXT,
                message TEXT, timestamp INTEGER
            )""")

    def record_transaction(self, tx: BridgeTransaction):
        self.transactions.append(tx)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO transactions (bridge, source_chain, dest_chain, token, amount, "
                "status, tx_hash, timestamp, completion_time, gas_used, fee) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tx.bridge, tx.source_chain, tx.dest_chain, tx.token, tx.amount,
                 tx.status, tx.tx_hash, tx.timestamp, tx.completion_time, tx.gas_used, tx.fee)
            )
        logger.info(f"Recorded: {tx.bridge} {tx.source_chain}→{tx.dest_chain} {tx.amount} {tx.token} [{tx.status}]")

    def complete_transaction(self, tx_hash: str, completion_time: int):
        for tx in self.transactions:
            if tx.tx_hash == tx_hash:
                tx.status = "completed"
                tx.completion_time = completion_time
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE transactions SET status='completed', completion_time=? WHERE tx_hash=?",
                        (completion_time, tx_hash)
                    )
                logger.info(f"Completed: {tx_hash[:16]}... (duration: {tx.duration}s)")
                return True
        return False

    def update_chain_status(self, bridge: str, chain: str, status: str, latency: int):
        now = int(time.time())
        if bridge not in self.chain_status:
            self.chain_status[bridge] = {}
        error_count = 0
        if chain in self.chain_status[bridge]:
            error_count = self.chain_status[bridge][chain].error_count
            if status == "down":
                error_count += 1
        self.chain_status[bridge][chain] = ChainStatus(
            chain=chain, bridge=bridge, status=status,
            latency=latency, last_check=now, error_count=error_count
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chain_status VALUES (?,?,?,?,?,?)",
                (chain, bridge, status, latency, now, error_count)
            )
        if status == "down":
            alert = {"severity": "critical", "bridge": bridge, "chain": chain,
                     "message": f"{bridge} on {chain} is DOWN", "timestamp": now}
            self.alerts.append(alert)
            logger.warning(f"ALERT: {alert['message']}")
        elif latency > 1800:
            alert = {"severity": "warning", "bridge": bridge, "chain": chain,
                     "message": f"{bridge} on {chain} high latency: {latency}s", "timestamp": now}
            self.alerts.append(alert)

    def get_bridge_stats(self) -> Dict:
        stats = {}
        for bridge_id, info in SUPPORTED_BRIDGES.items():
            bridge_txs = [t for t in self.transactions if t.bridge == bridge_id]
            completed = [t for t in bridge_txs if t.status == "completed"]
            avg_time = sum(t.duration for t in completed if t.duration) / len(completed) if completed else 0
            total_volume = sum(t.amount for t in bridge_txs)
            stats[bridge_id] = {
                "name": info["name"],
                "chains": info["chains"],
                "total_txs": len(bridge_txs),
                "completed": len(completed),
                "pending": sum(1 for t in bridge_txs if t.status == "pending"),
                "failed": sum(1 for t in bridge_txs if t.status == "failed"),
                "avg_completion_time": f"{avg_time:.0f}s",
                "success_rate": f"{len(completed) / len(bridge_txs):.2%}" if bridge_txs else "N/A",
                "total_volume": f"${total_volume:,.2f}",
            }
        return stats

    def get_chain_health(self) -> Dict:
        health = {}
        for bridge, chains in self.chain_status.items():
            for chain, status in chains.items():
                key = f"{bridge}/{chain}"
                health[key] = {
                    "status": status.status,
                    "latency": f"{status.latency}s",
                    "error_count": status.error_count,
                    "last_check": status.last_check,
                }
        return health

    def get_dashboard(self) -> Dict:
        return {
            "bridges_monitored": len(SUPPORTED_BRIDGES),
            "total_transactions": len(self.transactions),
            "total_alerts": len(self.alerts),
            "bridge_stats": self.get_bridge_stats(),
            "chain_health": self.get_chain_health(),
            "recent_alerts": self.alerts[-10:],
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MiMo Bridge Monitor")
    parser.add_argument("--bridges", nargs="+", default=["all"])
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--db", default="bridge_monitor.db")
    args = parser.parse_args()

    monitor = BridgeMonitor(db_path=args.db)
    bridges = SUPPORTED_BRIDGES if "all" in args.bridges else {b: SUPPORTED_BRIDGES[b] for b in args.bridges if b in SUPPORTED_BRIDGES}

    if args.dashboard:
        dash = monitor.get_dashboard()
        print(json.dumps(dash, indent=2))
    else:
        print(f"MiMo Bridge Monitor")
        print(f"Monitoring {len(bridges)} bridges:")
        for bid, info in bridges.items():
            print(f"  {info['name']}: {', '.join(info['chains'])}")


if __name__ == "__main__":
    main()
