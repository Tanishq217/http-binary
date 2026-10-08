# Annotated Wire Hexdump: Request, Response, and Extensibility

This document provides a complete, byte-for-byte annotated breakdown of a full HBIN/1 client/server transaction.

**Scenario:** Client fetches `/index.html` from `localhost:9000` via:
```bash
./bcurl -v localhost:9000/index.html
```
The server root holds a 15-byte file containing `<h1>hello</h1>\n` with timestamp `Tue, 14 Nov 2023 22:13:20 GMT`.

A total of three frames cross the wire (186 bytes):
1. **REQUEST** (Client → Server, 55 bytes)
2. **RESPONSE** (Server → Client, 108 bytes)
3. **DATA** (Server → Client, 23 bytes)

All multi-byte integers are unsigned big-endian (network byte order).

---

## 1. Client → Server: REQUEST Frame (55 bytes)

### Raw Hexdump
```
00000000  00 00 2f 01 01 00 00 01  01 00 0b 2f 69 6e 64 65  |../......../inde|
00000010  78 2e 68 74 6d 6c 01 00  0e 6c 6f 63 61 6c 68 6f  |x.html...localho|
00000020  73 74 3a 39 30 30 30 02  00 07 62 63 75 72 6c 2f  |st:9000...bcurl/|
00000030  31 03 00 03 2a 2f 2a                              |1...*/*|
```

### Byte Annotation

| Offset | Hex Bytes | Field | Description |
|---|---|---|---|
| `0..2` | `00 00 2f` | **Length** | 47 bytes of payload follow the 8-byte frame header |
| `3` | `01` | **Type** | `0x01` = `REQUEST` frame |
| `4` | `01` | **Flags** | `0x01` = `END_STREAM` (requests carry no body in v1) |
| `5..7` | `00 00 01` | **Stream ID** | Stream 1 (client's first transaction on connection) |
| `8` | `01` | **Method** | `1` = `GET` |
| `9..10` | `00 0b` | **Path Length** | 11 bytes |
| `11..21` | `2f 69 6e 64 65 78 2e 68 74 6d 6c` | **Path** | `/index.html` (ASCII UTF-8 string) |
| `22` | `01` | **Header ID** | ID `1` = `host` (Static Table indexed name) |
| `23..24` | `00 0e` | **Value Length** | 14 bytes |
| `25..38` | `6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30` | **Value** | `localhost:9000` |
| `39` | `02` | **Header ID** | ID `2` = `user-agent` (Static Table indexed name) |
| `40..41` | `00 07` | **Value Length** | 7 bytes |
| `42..48` | `62 63 75 72 6c 2f 31` | **Value** | `bcurl/1` |
| `49` | `03` | **Header ID** | ID `3` = `accept` (Static Table indexed name) |
| `50..51` | `00 03` | **Value Length** | 3 bytes |
| `52..54` | `2a 2f 2a` | **Value** | `*/*` |

**Verification:**
8 bytes (header) + 47 bytes (payload) = **55 bytes total**. The header block has no terminator; it consumes the remainder of the payload.

---

## 2. Server → Client: RESPONSE Frame (108 bytes)

### Raw Hexdump
```
00000000  00 00 64 02 00 00 00 01  00 c8 05 00 08 62 73 65  |..d..........bse|
00000010  72 76 65 2f 31 06 00 18  74 65 78 74 2f 68 74 6d  |rve/1...text/htm|
00000020  6c 3b 20 63 68 61 72 73  65 74 3d 75 74 66 2d 38  |l; charset=utf-8|
00000030  07 00 02 31 35 08 00 1d  54 75 65 2c 20 31 34 20  |...15...Tue, 14 |
00000040  4e 6f 76 20 32 30 32 33  20 32 32 3a 31 33 3a 32  |Nov 2023 22:13:2|
00000050  30 20 47 4d 54 09 00 14  22 66 2d 31 37 39 37 39  |0 GMT..."f-17979|
00000060  63 66 65 33 36 32 61 30  30 30 30 22              |cfe362a0000"|
```

### Byte Annotation

| Offset | Hex Bytes | Field | Description |
|---|---|---|---|
| `0..2` | `00 00 64` | **Length** | 100 bytes payload |
| `3` | `02` | **Type** | `0x02` = `RESPONSE` frame |
| `4` | `00` | **Flags** | `0x00` = No flags (`END_STREAM` is NOT set; DATA frames follow) |
| `5..7` | `00 00 01` | **Stream ID** | Stream 1 (echoed from the request) |
| `8..9` | `00 c8` | **Status Code** | `200` OK (`0x00c8` in big-endian) |
| `10` | `05` | **Header ID** | ID `5` = `server` |
| `11..12` | `00 08` | **Value Length** | 8 bytes |
| `13..20` | `62 73 65 72 76 65 2f 31` | **Value** | `bserve/1` |
| `21` | `06` | **Header ID** | ID `6` = `content-type` |
| `22..23` | `00 18` | **Value Length** | 24 bytes (`0x0018`) |
| `24..47` | `74 65 78 74 2f 68 74 6d 6c 3b 20 63 68 61 72 73 65 74 3d 75 74 66 2d 38` | **Value** | `text/html; charset=utf-8` |
| `48` | `07` | **Header ID** | ID `7` = `content-length` |
| `49..50` | `00 02` | **Value Length** | 2 bytes |
| `51..52` | `31 35` | **Value** | `15` (ASCII string) |
| `53` | `08` | **Header ID** | ID `8` = `last-modified` |
| `54..55` | `00 1d` | **Value Length** | 29 bytes (`0x001d`) |
| `56..84` | `54 75 65 2c ... 47 4d 54` | **Value** | `Tue, 14 Nov 2023 22:13:20 GMT` |
| `85` | `09` | **Header ID** | ID `9` = `etag` |
| `86..87` | `00 14` | **Value Length** | 20 bytes (`0x0014`) |
| `88..107` | `22 66 2d ... 30 22` | **Value** | `"f-17979cfe362a0000"` (includes enclosing double quotes) |

**Verification:**
8 bytes (header) + 2 bytes (status) + 98 bytes (5 encoded headers) = **108 bytes total**.

---

## 3. Server → Client: DATA Frame (23 bytes)

### Raw Hexdump
```
00000000  00 00 0f 03 01 00 00 01  3c 68 31 3e 68 65 6c 6c  |........<h1>hell|
00000010  6f 3c 2f 68 31 3e 0a                              |o</h1>.|
```

### Byte Annotation

| Offset | Hex Bytes | Field | Description |
|---|---|---|---|
| `0..2` | `00 00 0f` | **Length** | 15 bytes (`0x00000f`) |
| `3` | `03` | **Type** | `0x03` = `DATA` frame |
| `4` | `01` | **Flags** | `0x01` = `END_STREAM` (final frame of the stream) |
| `5..7` | `00 00 01` | **Stream ID** | Stream 1 |
| `8..22` | `3c 68 31 3e 68 65 6c 6c 6f 3c 2f 68 31 3e 0a` | **Payload** | `<h1>hello</h1>\n` (15 raw bytes) |

**Verification:**
8 bytes (header) + 15 bytes (payload) = **23 bytes total**. With `END_STREAM` set, the client knows the stream is complete and the connection is ready for the next request.

---

## 4. Unknown Frame Type Rule (Extensibility)

The protocol specification strictly mandates:
> **"A receiver meeting a frame type it does not know MUST skip it cleanly."**

Suppose an upgraded v2 server injects an experimental `PING` or `EXTENSION` frame (`Type = 0x55`, 5 bytes payload `hello`, `Stream ID = 0`) before the `RESPONSE`:

```
00000000  00 00 05 55 00 00 00 00  68 65 6c 6c 6f           |...U....hello|
```

| Offset | Hex Bytes | Meaning |
|---|---|---|
| `0..2` | `00 00 05` | Length = 5 bytes |
| `3` | `55` | Unrecognized frame type `0x55` |
| `4` | `00` | Flags = 0 |
| `5..7` | `00 00 00` | Stream ID = 0 |
| `8..12` | `68 65 6c 6c 6f` | 5 bytes payload |

**Receiver Action:**
1. A v1 peer reads the invariant 8-byte header and inspects `Length` (5) and `Type` (`0x55`).
2. Recognizing `0x55` as unknown, the receiver reads and discards exactly 5 bytes from the stream.
3. It emits no error, resets no connection, and immediately proceeds to read the next 8-byte frame header.
4. Stream synchronization is preserved perfectly.
