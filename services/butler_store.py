"""Durable activity metadata and delivery queue for the single-process butler."""

from __future__ import annotations
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import uuid


class ButlerStore:
    def __init__(self, path, *, retention_days=30, clock=time.time):
        self.path = Path(path)
        self.retention = retention_days * 86400
        self.clock = clock
        self.lock = asyncio.Lock()
        self.initialized = False
        self.last_prune = 0.0

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            if (
                not self.initialized
                or conn.execute("PRAGMA user_version").fetchone()[0] < 3
            ):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS guilds(id INTEGER PRIMARY KEY,name TEXT,first_seen REAL,last_seen REAL,audit_status TEXT DEFAULT 'not checked');
                    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,guild INTEGER,user INTEGER,name TEXT,kind TEXT,at REAL,action TEXT,target INTEGER,details TEXT);
                    CREATE INDEX IF NOT EXISTS event_day ON events(guild,at,kind);
                    CREATE TABLE IF NOT EXISTS ping_seen(id INTEGER PRIMARY KEY,count INTEGER,at REAL);
                    CREATE TABLE IF NOT EXISTS pings(id TEXT PRIMARY KEY,guild INTEGER,user INTEGER,at REAL,count INTEGER);
                    CREATE INDEX IF NOT EXISTS ping_window ON pings(guild,user,at);
                    CREATE TABLE IF NOT EXISTS cooldowns(id TEXT PRIMARY KEY,until REAL);
                    CREATE TABLE IF NOT EXISTS voice(id INTEGER PRIMARY KEY,guild INTEGER,user INTEGER,name TEXT,channel INTEGER,start REAL,end REAL,seen REAL,source TEXT,join_flag INTEGER);
                    CREATE UNIQUE INDEX IF NOT EXISTS active_voice ON voice(guild,user) WHERE end IS NULL;
                    CREATE INDEX IF NOT EXISTS voice_day ON voice(guild,start,end);
                    CREATE TABLE IF NOT EXISTS windows(id INTEGER PRIMARY KEY,guild INTEGER,start REAL,end REAL,seen REAL);
                    CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,guild INTEGER,recipient INTEGER,kind TEXT,payload TEXT,status TEXT,attempts INTEGER,due REAL,created REAL,sent REAL);
                    CREATE INDEX IF NOT EXISTS delivery_due ON outbox(status,due);
                    CREATE INDEX IF NOT EXISTS outbox_guild_kind ON outbox(guild,kind,created);
                    CREATE TABLE IF NOT EXISTS owner_inbox(reference TEXT PRIMARY KEY,state TEXT NOT NULL,actor INTEGER NOT NULL,updated REAL NOT NULL);
                """)
                # Existing ledgers do not contain delivery reasons or distinguish
                # accepted mentions from violations. Preserve their timestamps.
                for table, column in (("outbox", "failure TEXT"), ("pings", "allowed INTEGER DEFAULT 1")):
                    fields = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if column.split()[0] not in fields:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                conn.execute("CREATE TABLE IF NOT EXISTS antiping_policy(guild INTEGER PRIMARY KEY, data TEXT NOT NULL)")
                conn.execute("PRAGMA user_version=3")
                conn.commit()
                self.initialized = True
        except BaseException:
            conn.close()
            self.initialized = False
            raise
        return conn

    async def run(self, operation):
        async with self.lock:

            def work():
                with closing(self._connect()) as conn, conn:
                    now = self.clock()
                    if now - self.last_prune >= 3600:
                        cutoff = now - self.retention
                        conn.execute("DELETE FROM events WHERE at<?", (cutoff,))
                        conn.execute("DELETE FROM pings WHERE at<?", (now - 86400,))
                        conn.execute("DELETE FROM ping_seen WHERE at<?", (cutoff,))
                        conn.execute("DELETE FROM cooldowns WHERE until<?", (now,))
                        conn.execute(
                            "DELETE FROM voice WHERE end IS NOT NULL AND end<?",
                            (cutoff,),
                        )
                        conn.execute(
                            "DELETE FROM windows WHERE end IS NOT NULL AND end<?",
                            (cutoff,),
                        )
                        conn.execute(
                            "DELETE FROM outbox WHERE created<? AND status IN ('sent','failed')",
                            (cutoff,),
                        )
                        conn.execute("DELETE FROM owner_inbox WHERE reference NOT IN (SELECT id FROM outbox)")
                        self.last_prune = now
                    return operation(conn)

            task = asyncio.create_task(asyncio.to_thread(work))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if task.done():
                        break
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError
                    raise
            if cancelled:
                if not task.cancelled():
                    task.exception()
                raise asyncio.CancelledError
            return result

    @staticmethod
    def _event(
        conn, key, guild, user, name, kind, at, action="", target=0, details=None
    ):
        return (
            conn.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    guild,
                    user,
                    str(name)[:100],
                    kind,
                    at,
                    action,
                    target,
                    json.dumps(details or {}, ensure_ascii=False),
                ),
            ).rowcount
            > 0
        )

    @staticmethod
    def _cooldown(conn, key, now, seconds):
        row = conn.execute("SELECT until FROM cooldowns WHERE id=?", (key,)).fetchone()
        if row and row[0] > now:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now + seconds)
        )
        return True

    @staticmethod
    def _enqueue(conn, key, guild, recipient, kind, payload, now):
        if conn.execute("SELECT 1 FROM outbox WHERE id=?", (key,)).fetchone():
            return False
        if (
            conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='pending'"
            ).fetchone()[0]
            >= 1000
        ):
            if kind not in {"initial", "daily"}:
                return False
            # Keep the queue bounded while prioritizing the owner's ledger over
            # low-priority courtesy notices during a very busy period.
            removed = conn.execute(
                "UPDATE outbox SET status='failed',failure='queue_full' WHERE id=(SELECT id FROM outbox WHERE status='pending' AND kind IN ('warning','incident') ORDER BY created LIMIT 1)"
            ).rowcount
            if not removed:
                return False
        return (
            conn.execute(
                "INSERT OR IGNORE INTO outbox(id,guild,recipient,kind,payload,status,attempts,due,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    guild,
                    recipient,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    "pending",
                    0,
                    now,
                    now,
                ),
            ).rowcount
            > 0
        )

    async def begin(self, guild, name, voices):
        now = self.clock()

        def work(conn):
            conn.execute(
                "INSERT INTO guilds(id,name,first_seen,last_seen) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen",
                (guild, str(name)[:100], now, now),
            )
            conn.execute(
                "UPDATE windows SET end=seen WHERE guild=? AND end IS NULL", (guild,)
            )
            conn.execute(
                "UPDATE voice SET end=seen WHERE guild=? AND end IS NULL", (guild,)
            )
            conn.execute(
                "INSERT INTO windows(guild,start,seen) VALUES(?,?,?)", (guild, now, now)
            )
            for user, display, channel in voices:
                conn.execute(
                    "INSERT INTO voice(guild,user,name,channel,start,seen,source,join_flag) VALUES(?,?,?,?,?,?,?,0)",
                    (guild, user, str(display)[:100], channel, now, now, "observed"),
                )

        await self.run(work)

    async def pause(self, guild=None):
        now = self.clock()

        def work(conn):
            scope = " AND guild=?" if guild is not None else ""
            params = (now, now, guild) if guild is not None else (now, now)
            conn.execute(
                "UPDATE voice SET end=?,seen=? WHERE end IS NULL" + scope, params
            )
            conn.execute(
                "UPDATE windows SET end=?,seen=? WHERE end IS NULL" + scope, params
            )

        await self.run(work)

    async def retire(self, guild):
        await self.pause(guild)
        await self.run(
            lambda conn: conn.execute(
                "UPDATE outbox SET status='failed',failure='guild_unavailable' WHERE guild=? AND status='pending'",
                (guild,),
            ).rowcount
        )

    async def heartbeat(self, guild, voices):
        now = self.clock()

        def work(conn):
            conn.execute("UPDATE guilds SET last_seen=? WHERE id=?", (now, guild))
            conn.execute(
                "UPDATE windows SET seen=? WHERE guild=? AND end IS NULL", (now, guild)
            )
            active = {
                r["user"]: r
                for r in conn.execute(
                    "SELECT * FROM voice WHERE guild=? AND end IS NULL", (guild,)
                )
            }
            present = {u: (n, c) for u, n, c in voices}
            for user, row in active.items():
                if user not in present or row["channel"] != present[user][1]:
                    conn.execute("UPDATE voice SET end=seen WHERE id=?", (row["id"],))
                else:
                    conn.execute("UPDATE voice SET seen=? WHERE id=?", (now, row["id"]))
            for user, (name, channel) in present.items():
                if user not in active or active[user]["channel"] != channel:
                    conn.execute(
                        "INSERT INTO voice(guild,user,name,channel,start,seen,source,join_flag) VALUES(?,?,?,?,?,?,?,0)",
                        (guild, user, str(name)[:100], channel, now, now, "observed"),
                    )

        await self.run(work)

    async def voice_change(self, guild, user, name, before, after):
        if before == after:
            return
        now = self.clock()

        def work(conn):
            row = conn.execute(
                "SELECT id,channel,source FROM voice WHERE guild=? AND user=? AND end IS NULL",
                (guild, user),
            ).fetchone()
            if row and row["channel"] == after:
                if row["source"] == "observed":
                    # A heartbeat may see the gateway-updated cache before this
                    # listener acquires storage. Preserve the real transition.
                    conn.execute(
                        "UPDATE voice SET source=?,join_flag=? WHERE id=?",
                        (
                            "move" if before is not None else "gateway",
                            int(before is None),
                            row["id"],
                        ),
                    )
                return
            if row:
                conn.execute(
                    "UPDATE voice SET end=?,seen=? WHERE id=?", (now, now, row["id"])
                )
            if after is not None:
                conn.execute(
                    "INSERT INTO voice(guild,user,name,channel,start,seen,source,join_flag) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        guild,
                        user,
                        str(name)[:100],
                        after,
                        now,
                        now,
                        "move" if before is not None else "gateway",
                        int(before is None),
                    ),
                )

        await self.run(work)

    async def member(self, guild, user, name, kind, joined_at=None, *, imported=False):
        now = self.clock()

        def work(conn):
            if kind == "join":
                at = joined_at if joined_at is not None else now
                conn.execute(
                    "DELETE FROM cooldowns WHERE id=?", (f"leave:{guild}:{user}",)
                )
                return self._event(
                    conn,
                    f"join:{guild}:{user}:{at}",
                    guild,
                    user,
                    name,
                    "join",
                    at,
                    details={
                        "source": "current member timestamp" if imported else "gateway"
                    },
                )
            if not self._cooldown(conn, f"leave:{guild}:{user}", now, 5):
                return False
            conn.execute(
                "UPDATE voice SET end=?,seen=? WHERE guild=? AND user=? AND end IS NULL",
                (now, now, guild, user),
            )
            return self._event(
                conn, f"leave:{uuid.uuid4().hex}", guild, user, name, "leave", now
            )

        return await self.run(work)

    async def import_joins(self, guild, items):
        def work(conn):
            for user, name, stamp in items:
                self._event(
                    conn,
                    f"join:{guild}:{user}:{stamp}",
                    guild,
                    user,
                    name,
                    "join",
                    stamp,
                    details={"source": "current member timestamp"},
                )

        await self.run(work)

    async def policy(self, guild, defaults, update=None):
        def work(conn):
            row = conn.execute("SELECT data FROM antiping_policy WHERE guild=?", (guild,)).fetchone()
            policy = dict(defaults) if row is None else {**defaults, **json.loads(row[0])}
            if update is not None:
                update(policy)
                conn.execute(
                    "INSERT OR REPLACE INTO antiping_policy VALUES(?,?)", (guild, json.dumps(policy))
                )
            return policy

        return await self.run(work)

    async def ping(self, guild, user, name, message, seconds):
        now = self.clock()
        key = f"ping:{message}:1"

        def work(conn):
            # The write serializes the decision even if multiple SQLite clients
            # open this ledger. A duplicate can still report policy ownership.
            inserted = conn.execute(
                "INSERT OR IGNORE INTO ping_seen VALUES(?,1,?)", (message, now)
            ).rowcount
            if not inserted:
                violation = conn.execute("SELECT 1 FROM events WHERE id=?", (key,)).fetchone()
                return {"duplicate": True} if violation else None
            accepted = not conn.execute(
                "SELECT 1 FROM pings WHERE guild=? AND user=? AND at>? AND allowed=1 LIMIT 1",
                (guild, user, now - seconds),
            ).fetchone()
            conn.execute(
                "INSERT INTO pings(id,guild,user,at,count,allowed) VALUES(?,?,?,?,1,?)",
                (key, guild, user, now, accepted),
            )
            if accepted:
                return None
            total = conn.execute(
                "SELECT COUNT(*) FROM pings WHERE guild=? AND user=? AND at>?",
                (guild, user, now - seconds),
            ).fetchone()[0]
            notice = self._cooldown(conn, f"ping-notice:{guild}:{user}", now, seconds)
            self._event(
                conn, key, guild, user, name, "ping_violation", now, target=message,
                details={"mentions_in_window": total, "deleted": "pending"},
            )
            return {"key": key, "notice": notice, "total": total, "seconds": seconds}

        return await self.run(work)

    async def complete_ping(
        self, guild, user, name, guild_name, decision, deleted, reason, *, dm_user=True, warn_only=False
    ):
        now = self.clock()

        def work(conn):
            details = {
                "mentions_in_window": decision["total"],
                "deleted": deleted,
                "delete_result": reason,
                "cooldown_seconds": decision["seconds"],
                "warn_only": warn_only,
            }
            conn.execute(
                "UPDATE events SET details=? WHERE id=?",
                (json.dumps(details), decision["key"]),
            )
            if decision["notice"]:
                payload = {
                    "name": str(name)[:100],
                    "server": str(guild_name)[:100],
                    "user": user,
                    **details,
                }
                if dm_user:
                    self._enqueue(
                        conn,
                        "warning:" + decision["key"],
                        guild,
                        user,
                        "warning",
                        payload,
                        now,
                    )
                self._enqueue(
                    conn,
                    "incident:" + decision["key"],
                    guild,
                    None,
                    "incident",
                    payload,
                    now,
                )

        await self.run(work)

    async def audit(
        self, guild, identifier, user, name, action, target, at, flags, *, notify=True
    ):
        now = self.clock()

        def work(conn):
            added = self._event(
                conn,
                f"audit:{guild}:{identifier}",
                guild,
                user,
                name,
                "audit",
                at,
                action,
                target,
                {"flags": flags},
            )
            if not added:
                return False
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE guild=? AND user=? AND kind='audit' AND action=? AND at>?",
                (guild, user, action, now - 600),
            ).fetchone()[0]
            rapid = (
                action in {"channel_delete", "role_delete", "ban", "kick"}
                and count >= 3
            )
            if (
                notify
                and (flags or rapid)
                and self._cooldown(
                    conn, f"audit-alert:{guild}:{user}:{action}", now, 3600
                )
            ):
                self._enqueue(
                    conn,
                    f"audit-alert:{guild}:{identifier}",
                    guild,
                    None,
                    "audit_alert",
                    {
                        "user": user,
                        "name": str(name)[:100],
                        "action": action,
                        "target": target,
                        "flags": flags,
                        "recent_count": count,
                    },
                    now,
                )
            return True

        return await self.run(work)

    async def audit_status(self, guild, status):
        await self.run(
            lambda conn: conn.execute(
                "UPDATE guilds SET audit_status=? WHERE id=?", (status[:200], guild)
            ).rowcount
        )

    async def moderation(self, identifier, guild, actor, target, action, detail, recipients):
        now = self.clock()
        def work(conn):
            self._event(conn, f"moderation:{identifier}", guild, actor, str(actor),
                        "moderation", now, action, target, {"summary": detail[:1000]})
            for recipient in recipients:
                self._enqueue(conn, f"moderation:{identifier}:{recipient}", guild, recipient,
                              "moderation", {"summary": detail[:1500]}, now)
        await self.run(work)

    async def enqueue_report(self, guild, day, kind):
        now = self.clock()
        return await self.run(
            lambda conn: self._enqueue(
                conn, f"{kind}:{guild}:{day}", guild, None, kind, {"day": day}, now
            )
        )

    async def enqueue_daily_reports(self, report_dates):
        now = self.clock()

        def work(conn):
            for guild, day in report_dates:
                self._enqueue(conn, f"daily:{guild}:{day}", guild, None, "daily", {"day": day}, now)

        await self.run(work)

    async def assign_owner_delivery(self, job, application_owner, guild_owner):
        """Give each owner a durable delivery record before attempting either DM."""
        def work(conn):
            changed = conn.execute(
                "UPDATE outbox SET recipient=? WHERE id=? AND recipient IS NULL AND status='pending'",
                (application_owner, job['id']),
            ).rowcount
            if not changed or application_owner == guild_owner:
                return None
            # Each accepted owner event reserves at most one additional delivery.
            # Copy its original deadline and age so a restart never extends retention.
            sibling = f"{job['id']}:guild-owner"
            conn.execute(
                """INSERT INTO outbox(id,guild,recipient,kind,payload,status,attempts,due,created)
                   SELECT ?,guild,?,kind,payload,'pending',0,due,created FROM outbox WHERE id=?""",
                (sibling, guild_owner, job['id']),
            )
            return dict(conn.execute("SELECT * FROM outbox WHERE id=?", (sibling,)).fetchone())

        return await self.run(work)

    async def guilds(self):
        return await self.run(
            lambda conn: [dict(r) for r in conn.execute("SELECT * FROM guilds")]
        )

    async def due(self, limit=10):
        now = self.clock()

        def expire(conn):
            conn.execute(
                "UPDATE outbox SET status='failed',failure='expired' WHERE status='pending' AND created < ? - CASE WHEN kind IN ('warning','incident') THEN 10800 ELSE 172800 END",
                (now,),
            )
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM outbox WHERE status='pending' AND due<=? ORDER BY CASE kind WHEN 'initial' THEN 0 WHEN 'daily' THEN 1 ELSE 2 END,created LIMIT ?",
                    (now, limit),
                )
            ]

        return await self.run(expire)

    async def postpone(self, key):
        await self.run(
            lambda conn: conn.execute(
                "UPDATE outbox SET due=? WHERE id=?", (self.clock() + 60, key)
            ).rowcount
        )

    async def delivered(self, key):
        await self.run(
            lambda conn: conn.execute(
                "UPDATE outbox SET status='sent',sent=?,failure=NULL WHERE id=?", (self.clock(), key)
            ).rowcount
        )

    async def failed(self, key, reason="unexpected", *, permanent=False, retry_after=0):
        now = self.clock()

        def work(conn):
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE id=?", (key,)
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            terminal = permanent or attempts >= 3
            conn.execute(
                "UPDATE outbox SET attempts=?,status=?,due=?,failure=? WHERE id=?",
                (
                    attempts,
                    "failed" if terminal else "pending",
                    now + max(retry_after, 60 * 5 ** (attempts - 1)),
                    reason,
                    key,
                ),
            )

            return terminal

        return await self.run(work)

    async def delivery_health(self, guild):
        return await self.run(lambda conn: [
            (row["status"], row["failure"], row["total"])
            for row in conn.execute(
                "SELECT status,failure,COUNT(*) AS total FROM outbox WHERE guild=? GROUP BY status,failure",
                (guild,),
            )
        ])

    async def owner_report_deliveries(self, guild, recipient):
        def work(conn):
            last_sent = conn.execute(
                "SELECT MAX(sent) FROM outbox WHERE guild=? AND recipient=? AND kind IN ('initial','daily') AND status='sent'",
                (guild, recipient),
            ).fetchone()[0]
            failed = [dict(row) for row in conn.execute(
                """SELECT id,payload,failure,created FROM outbox
                   WHERE guild=? AND recipient=? AND kind IN ('initial','daily') AND status='failed'
                   ORDER BY created DESC LIMIT 5""", (guild, recipient),
            )]
            return last_sent, failed

        return await self.run(work)

    async def retry_owner_report(self, guild, recipient, reference):
        now = self.clock()

        def work(conn):
            changed = conn.execute(
                """UPDATE outbox SET status='pending',attempts=0,due=?,failure=NULL WHERE id=?
                   AND guild=? AND recipient=? AND kind IN ('initial','daily') AND status='failed'
                   AND created>=? AND COALESCE(failure,'') NOT IN ('owner_changed','expired')
                   AND (SELECT COUNT(*) FROM outbox WHERE status='pending') < 1000""",
                (now, reference, guild, recipient, now - 172800),
            ).rowcount
            return bool(changed)

        return await self.run(work)

    async def allow_delivery_test(self, recipient):
        now = self.clock()
        return await self.run(lambda conn: self._cooldown(conn, f"owner-dm-test:{recipient}", now, 60))

    async def inbox_entries(self, guild, *, reference=None, state=None):
        now = self.clock()
        return await self.run(lambda conn: [dict(row) for row in conn.execute(
            """SELECT o.id,o.kind,o.created,o.payload,COALESCE(i.state,'needs_review') state,i.actor,i.updated
               FROM outbox o LEFT JOIN owner_inbox i ON o.id=i.reference
               WHERE o.guild=? AND o.created>=? AND o.kind IN ('audit_alert','incident')
                 AND o.id NOT LIKE '%:guild-owner' AND (? IS NULL OR o.id=?)
                 AND (? IS NULL OR COALESCE(i.state,'needs_review')=?)
                 AND (? IS NOT NULL OR ? IS NOT NULL OR COALESCE(i.state,'needs_review')<>'resolved')
               ORDER BY o.created DESC,o.id DESC LIMIT 8""",
            (guild, now - self.retention, reference, reference, state, state, reference, state),
        )])

    async def set_inbox_state(self, guild, reference, state, actor):
        if state not in {"needs_review", "acknowledged", "resolved"}:
            raise ValueError("Unsupported inbox state")
        now = self.clock()
        return await self.run(lambda conn: bool(conn.execute(
            """INSERT INTO owner_inbox(reference,state,actor,updated)
               SELECT id,?,?,? FROM outbox WHERE guild=? AND id=? AND created>=?
               AND kind IN ('audit_alert','incident') AND id NOT LIKE '%:guild-owner'
               ON CONFLICT(reference) DO UPDATE SET state=excluded.state,actor=excluded.actor,updated=excluded.updated""",
            (state, actor, now, guild, reference, now - self.retention),
        ).rowcount))

    async def health(self):
        return await self.run(lambda conn: conn.execute("PRAGMA quick_check(1)").fetchone()[0])

    async def report(self, guild, start, end):
        now = self.clock()

        def work(conn):
            meta = conn.execute("SELECT * FROM guilds WHERE id=?", (guild,)).fetchone()
            counts = {
                r["kind"]: r["n"]
                for r in conn.execute(
                    "SELECT kind,COUNT(*) n FROM events WHERE guild=? AND at>=? AND at<? GROUP BY kind",
                    (guild, start, end),
                )
            }
            members = [
                dict(r)
                for r in conn.execute(
                    "SELECT user,name,kind,at,details FROM events WHERE guild=? AND at>=? AND at<? AND kind IN ('join','leave') ORDER BY at LIMIT 1000",
                    (guild, start, end),
                )
            ]
            voices = [
                dict(r)
                for r in conn.execute(
                    """SELECT user,MAX(name) name,
                SUM(MAX(0,MIN(COALESCE(end,?),?)-MAX(start,?))) seconds,
                SUM(CASE WHEN join_flag=1 AND start>=? AND start<? THEN 1 ELSE 0 END) joins,
                SUM(CASE WHEN source='move' AND start>=? AND start<? THEN 1 ELSE 0 END) moves,
                MAX(CASE WHEN end IS NULL AND ?>=? THEN 1 ELSE 0 END) active
                FROM voice WHERE guild=? AND start<? AND COALESCE(end,?)>? GROUP BY user ORDER BY seconds DESC LIMIT 500""",
                    (
                        now,
                        end,
                        start,
                        start,
                        end,
                        start,
                        end,
                        end,
                        now,
                        guild,
                        end,
                        now,
                        start,
                    ),
                )
            ]
            audits = [
                dict(r)
                for r in conn.execute(
                    "SELECT user,MAX(name) name,action,COUNT(*) n FROM events WHERE guild=? AND kind='audit' AND at>=? AND at<? GROUP BY user,action ORDER BY n DESC LIMIT 200",
                    (guild, start, end),
                )
            ]
            details = [
                dict(r)
                for r in conn.execute(
                    "SELECT user,name,action,target,at,details FROM events WHERE guild=? AND kind='audit' AND at>=? AND at<? ORDER BY at DESC LIMIT 200",
                    (guild, start, end),
                )
            ]
            incidents = [
                dict(r)
                for r in conn.execute(
                    "SELECT user,name,target,at,details FROM events WHERE guild=? AND kind='ping_violation' AND at>=? AND at<? ORDER BY at LIMIT 200",
                    (guild, start, end),
                )
            ]
            coverage = conn.execute(
                "SELECT COALESCE(SUM(MAX(0,MIN(COALESCE(end,?),?)-MAX(start,?))),0) FROM windows WHERE guild=? AND start<? AND COALESCE(end,?)>?",
                (now, end, start, guild, end, now, start),
            ).fetchone()[0]
            voice_total = conn.execute(
                "SELECT COALESCE(SUM(MAX(0,MIN(COALESCE(end,?),?)-MAX(start,?))),0) FROM voice WHERE guild=? AND start<? AND COALESCE(end,?)>?",
                (now, end, start, guild, end, now, start),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT status,COUNT(*) n FROM outbox WHERE guild=? GROUP BY status",
                (guild,),
            ).fetchall()
            return {
                "guild": (
                    dict(meta)
                    if meta
                    else {
                        "id": guild,
                        "name": str(guild),
                        "first_seen": now,
                        "audit_status": "not checked",
                    }
                ),
                "counts": counts,
                "members": members,
                "voices": voices,
                "audits": audits,
                "audit_details": details,
                "moderation": [dict(row) for row in conn.execute(
                    "SELECT user,target,action,at,details FROM events WHERE guild=? AND kind='moderation' AND at>=? AND at<? ORDER BY at LIMIT 500",
                    (guild, start, end))],
                "incidents": incidents,
                "coverage_seconds": coverage,
                "voice_total_seconds": voice_total,
                "delivery": {r["status"]: r["n"] for r in pending},
            }

        return await self.run(work)
