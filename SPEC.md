# HBIN/1 — HTTP, in Binary
## Protocol Specification

A minimal, high-performance binary transport protocol for file transfers over a single persistent TCP connection. 

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119.

All integers on the wire are **unsigned, big-endian (network byte order)**. All text is encoded as **UTF-8** unless specifically designated as ASCII.

---

## 1. Connection Lifecycle

1. **Direct Framing:** The client establishes a standard TCP connection. There is **no connection preface, magic string, or TLS-style handshake**; the very first byte transmitted on the wire is byte 0 of the first frame header.
2. **Persistent Connection (Keep-Alive):** A connection remains open after completing a request/response transaction. Clients SHOULD reuse an existing open connection for subsequent requests rather than closing and opening a new TCP socket.
3. **Sequential Exchange (Stop-and-Wait):** In version 1, exchanges are strictly serialized. A client MUST NOT send a second request frame until the response for the prior request has fully terminated (signaled by a frame with the `END_STREAM` flag). The server processes and responds to requests in the exact order received.
4. **Clean Termination:** Either endpoint MAY terminate the connection at any clean frame boundary. Servers MAY close idle connections after an inactivity timeout. A socket close occurring mid-frame or before receiving `END_STREAM` is an abnormal termination.

---

## 2. Frame Header Format

Every transmission consists of an 8-byte fixed header followed by `Length` bytes of payload:

```
  0                   1                   2                   3
  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
 +-----------------------------------------------+---------------+
 |                    Length                     |     Type      |
 |                   (24 bits)                   |   (8 bits)    |
 +---------------+-------------------------------+---------------+
 |     Flags     |                   Stream ID                   |
 |   (8 bits)    |                   (24 bits)                   |
 +---------------+-----------------------------------------------+
```

```
 byte 0       1       2   | byte 3 | byte 4 | byte 5      6       7
+-------------------------+--------+--------+-----------------------+
|         Length          |  Type  | Flags  |       Stream ID       |
|        (24 bits)        | (8 bit)| (8 bit)|       (24 bits)       |
+-------------------------+--------+--------+-----------------------+
```

### Field Definitions

| Field | Width | Description |
|---|---|---|
| **Length** | 24 bits | Size of the frame payload in bytes (0 to 16,777,215). Does not count the 8-byte header. |
| **Type** | 8 bits | Identifies the frame purpose and payload structure (Section 3). |
| **Flags** | 8 bits | Control flags for the frame. Bit 0 (`0x01`) is `END_STREAM`. Bits 1–7 are reserved. |
| **Stream ID** | 24 bits | Numerical identifier for an exchange. Assigned by the client starting at 1. `0` is reserved. |

### Architectural Defense of Field Widths

* **Why an 8-byte header instead of HTTP/2's 9-byte header?**
  HTTP/2 allocated 24 bits for Length, 8 bits for Type, 8 bits for Flags, and 32 bits for Stream Identifier (using 1 reserved bit + 31 bits ID). This totals 72 bits (9 bytes). A 9-byte header crosses 64-bit machine boundaries awkwardly, complicates zero-copy framing, and causes memory unalignment. By utilizing a 24-bit Stream ID, HBIN/1 packs the entire header into exactly 8 bytes (two 32-bit words, 64-bit aligned). A 24-bit ID allows up to 16,777,215 distinct requests on a single connection—far exceeding the practical lifetime of any single TCP session.
* **Why a 24-bit Length with a 16 KiB default limit?**
  A 16-bit Length field would permanently restrict payloads to 64 KiB. Giving Length 24 bits preserves compatibility for future streaming upgrades up to 16 MiB without changing the frame header layout. Meanwhile, the v1 payload ceiling of 16,384 bytes is an enforcement policy (Section 6) that keeps endpoint memory buffers bounded.
* **Why an 8-bit Type?**
  An 8-bit field accommodates 256 unique frame types. Only three are defined in v1, leaving 253 type slots available for protocol upgrades.
* **Flags Reservation:**
  Senders MUST set undefined flags (bits 1–7) to zero. Receivers MUST ignore undefined flags. This allows future backward-compatible flags.

---

## 3. Frame Types

| Type ID | Name | Direction | Payload Description |
|---|---|---|---|
| `0x01` | **REQUEST** | Client → Server | `method:u8` `path_len:u16` `path` `headers` |
| `0x02` | **RESPONSE** | Server → Client | `status:u16` `headers` |
| `0x03` | **DATA** | Server → Client | Raw response body payload chunk |
| `0x04`–`0xFF` | *Reserved* | Either | Reserved for future protocol versions |

### Unknown Frame Rule (Extensibility Requirement)

> **Mandatory Rule:** A receiver encountering a frame with an unrecognized `Type` MUST read and discard exactly `Length` bytes of payload, emit no protocol error, and continue processing subsequent frames seamlessly.

Because all frames share the invariant 8-byte header structure, any endpoint can skip unknown frame payloads without losing stream alignment. This guarantees that future protocol extensions (e.g., PING, SETTINGS, PRIORITY) will not break existing v1 peers.

### Method Identifiers
* `1` = `GET` (Fetch resource with body)
* `2` = `HEAD` (Fetch resource metadata/headers only; no response DATA frames)
* Other method numbers are unsupported and result in status `405 Method Not Allowed`.

### Path Encoding
* Path MUST start with an ASCII `/` (`0x2F`).
* Path MUST NOT contain NUL bytes (`0x00`).
* Path MUST be valid UTF-8. It is raw, not percent-encoded.

---

## 4. Header Block & Compression

The `headers` segment in `REQUEST` and `RESPONSE` frames is a contiguous sequence of zero or more length-prefixed header entries. There is no top-level header count; entries are parsed until the frame payload is exhausted.

Two encoding formats are supported (inspired by HPACK):

### 1. Indexed Header (Known Name)
When the header name is present in the Static Header Table:
```
+---------------+-------------------------------+-----------------------+
|  id (1..255)  |       value_len (16 bits)     |         value         |
|   (1 byte)    |           (2 bytes)           |   (value_len bytes)   |
+---------------+-------------------------------+-----------------------+
```

### 2. Literal Header (Custom / Extension Name)
When the header name is not in the Static Header Table (`id = 0`):
```
+--------+---------------+-------------------+---------------------+--------------------+
|  0x00  |   name_len    |       name        | value_len (16 bits) |       value        |
| (1 B)  |    (1 B)      | (name_len bytes)  |      (2 bytes)      | (value_len bytes)  |
+--------+---------------+-------------------+---------------------+--------------------+
```

### Static Header Table

| ID | Header Name | ID | Header Name |
|---|---|---|---|
| `1` | `host` | `6` | `content-type` |
| `2` | `user-agent` | `7` | `content-length` |
| `3` | `accept` | `8` | `last-modified` |
| `4` | `if-none-match` | `9` | `etag` |
| `5` | `server` | `10` | `allow` |

* IDs `11` through `255` are reserved for future protocol revisions.
* Receivers encountering an unknown indexed header ID (`11..255`) MUST read `value_len`, discard `value_len` bytes, and continue decoding subsequent headers.
* Literal header names MUST be lowercase ASCII. Header values are UTF-8 text strings.

---

## 5. Client / Server Exchange

### Request Transaction
1. The client sends exactly one `REQUEST` frame with the `END_STREAM` flag (`0x01`) enabled.
2. In v1, requests do not carry a body.
3. The client SHOULD provide `host`, `user-agent`, and `accept` headers.

### Response Transaction
1. The server replies with one `RESPONSE` frame carrying the HTTP status code and response headers.
2. If the request was a `HEAD` request, a `304 Not Modified`, or the file has 0 bytes, the server sets `END_STREAM` on the `RESPONSE` frame itself.
3. Otherwise, the server transmits the file content across zero or more `DATA` frames (each payload at most 16,384 bytes).
4. The server MUST set `END_STREAM` on the final `DATA` frame.
5. All response frames echo the `Stream ID` of the triggering request.

### File Resolution & Path Security
* The server resolves request paths against a designated root directory.
* The server strips the leading `/`. If the resulting relative path is empty or ends in `/`, `index.html` is appended.
* The canonical absolute path is resolved using filesystem realpaths. Any path attempting directory traversal outside the root directory (e.g., via `..` or symlinks pointing outside) MUST be rejected and treated as absent (status `404`).
* Only regular files are served. Directories and special devices return `404`.

### Protocol Status Codes

| Status | Meaning & Server Behavior |
|---|---|
| `200` | Resource found. Headers: `server`, `content-type`, `content-length`, `last-modified`, `etag`. Followed by file content in `DATA` frames. |
| `304` | Not Modified. Triggered when `if-none-match` matches the file `etag`. No body payload (`END_STREAM` on `RESPONSE`). |
| `400` | Bad Request / Malformed frame. Frame is complete but violates protocol rules (see below). Server sends 400 and **keeps connection open**. |
| `404` | Not Found. Target regular file does not exist under root directory. |
| `405` | Method Not Allowed. Request method was not `1` (GET) or `2` (HEAD). Response includes `allow: GET, HEAD`. |
| `500` | Internal Server Error. Unexpected server-side failure. |

All error responses (`4xx` and `5xx`) include a short `text/plain` diagnostic body explaining the error.

### Error Handling: 400 Malformed vs Socket Close
The server emits a `400` response and **keeps the connection open** for:
- Known frame type invalid in current direction (e.g., client sending `RESPONSE` or `DATA`).
- `REQUEST` frame missing `END_STREAM` or with `Stream ID == 0`.
- Payload too short for fixed fields.
- Header block parsing error (truncated entry, non-ASCII name).
- Path invalid (missing leading `/`, contains NUL byte, invalid UTF-8).

**Exception:** If a received frame header specifies a `Length` exceeding 16,384 bytes, the server cannot safely determine framing synchronization without unbounded buffering. The server responds with `400` and closes the TCP connection.

---

## 6. Constraints & Protocol Limits

* **Maximum Frame Payload:** 16,384 bytes (16 KiB) for all frames in both directions.
* **Maximum Path Length:** 8,192 bytes.
* **Maximum Header Value Length:** 65,535 bytes.
* **Stream ID Range:** `1` to `16,777,215` (`0x000001` to `0xFFFFFF`). `0` is reserved for future connection-level frames.
