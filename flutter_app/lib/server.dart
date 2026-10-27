// FileBridge server for Android: same HTTP API and web UI as the desktop
// Python version (filebridge.py), so any browser can connect to the phone.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:mime/mime.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import 'zip_stream.dart';

const appVersion = '0.1.0';
const permLabels = {
  'read': 'View & download',
  'upload': 'View & upload',
  'full': 'Full access',
};
const _levels = {
  'read': {'read'},
  'upload': {'read', 'upload'},
  'full': {'read', 'upload', 'full'},
  'owner': {'read', 'upload', 'full', 'owner'},
};

class AccessRequest {
  AccessRequest({required this.id, required this.name, required this.ip, required this.ua})
      : time = DateTime.now();
  final String id;
  String name;
  final String ip;
  final String ua;
  final DateTime time;
  String status = 'pending';
  String? token;
}

class Device {
  Device({required this.id, required this.name, required this.ip, required this.perm,
      required this.approvedAt, required this.lastSeen});
  final String id;
  String name;
  String ip;
  String perm;
  DateTime approvedAt;
  DateTime lastSeen;

  bool get online => DateTime.now().difference(lastSeen).inSeconds < 40;

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'ip': ip,
        'perm': perm,
        'approved_at': approvedAt.millisecondsSinceEpoch ~/ 1000,
        'last_seen': lastSeen.millisecondsSinceEpoch ~/ 1000,
      };

  static Device fromJson(Map<String, dynamic> j) => Device(
        id: '${j['id']}',
        name: '${j['name']}',
        ip: '${j['ip'] ?? ''}',
        perm: permLabels.containsKey(j['perm']) ? j['perm'] as String : 'read',
        approvedAt: DateTime.fromMillisecondsSinceEpoch(((j['approved_at'] ?? 0) as num).toInt() * 1000),
        lastSeen: DateTime.fromMillisecondsSinceEpoch(((j['last_seen'] ?? 0) as num).toInt() * 1000),
      );
}

class HttpError implements Exception {
  HttpError(this.code, this.message);
  final int code;
  final String message;
  @override
  String toString() => message;
}

String? _s(Object? v) => v is String ? v : null;

String cleanName(String? raw) {
  var n = (raw ?? '').trim();
  for (final ch in const ['<', '>', ':', '"', '|', '?', '*']) {
    n = n.replaceAll(ch, '_');
  }
  if (n.isEmpty || n == '.' || n == '..' || n.contains('/') || n.contains('\\') ||
      n.contains('\u0000') || n.length > 255) {
    throw HttpError(400, 'Invalid name');
  }
  return n;
}

class FileBridgeServer extends ChangeNotifier {
  FileBridgeServer(this.root);

  String root;
  String deviceName = 'Android phone';
  int port = 0;
  List<String> ips = [];
  final Map<String, AccessRequest> pending = {};
  final Map<String, Device> devices = {}; // token -> device
  void Function(AccessRequest r)? onNewRequest;

  HttpServer? _server;
  String _joinHtml = '';
  String _explorerHtml = '';
  File? _devicesFile;
  File? _configFile;
  final Random _rand = Random.secure();

  bool get running => _server != null;
  String get cookieName => 'fb_token_$port';
  List<AccessRequest> get waiting => pending.values.where((r) => r.status == 'pending').toList();
  List<String> get urls => [for (final ip in ips) 'http://$ip:$port'];

  String _token(int bytes) =>
      base64Url.encode(List<int>.generate(bytes, (_) => _rand.nextInt(256))).replaceAll('=', '');

  // ------------------------------------------------------------------ setup
  Future<void> init() async {
    _joinHtml = await rootBundle.loadString('assets/web/join.html');
    _explorerHtml = await rootBundle.loadString('assets/web/explorer.html');
    final dir = await getApplicationSupportDirectory();
    _devicesFile = File(p.join(dir.path, 'devices.json'));
    _configFile = File(p.join(dir.path, 'config.json'));
    try {
      final j = jsonDecode(await _devicesFile!.readAsString()) as Map<String, dynamic>;
      j.forEach((tok, v) => devices[tok] = Device.fromJson(v as Map<String, dynamic>));
    } catch (_) {}
    try {
      final c = jsonDecode(await _configFile!.readAsString()) as Map<String, dynamic>;
      final r = _s(c['root']);
      if (r != null && Directory(r).existsSync()) root = r;
      final n = _s(c['name']);
      if (n != null && n.trim().isNotEmpty) deviceName = n;
    } catch (_) {}
  }

  Future<void> _saveDevices() async {
    try {
      await _devicesFile?.writeAsString(jsonEncode({for (final e in devices.entries) e.key: e.value.toJson()}));
    } catch (_) {}
  }

  Future<void> _saveConfig() async {
    try {
      await _configFile?.writeAsString(jsonEncode({'root': root, 'name': deviceName}));
    } catch (_) {}
  }

  Future<void> start({int preferred = 8000}) async {
    if (_server != null) return;
    for (var pt = preferred; pt < preferred + 20; pt++) {
      try {
        _server = await HttpServer.bind(InternetAddress.anyIPv4, pt);
        port = pt;
        break;
      } on SocketException {
        continue;
      }
    }
    if (_server == null) throw Exception('No free port between $preferred and ${preferred + 19}');
    await refreshIps();
    _server!.listen(_handle, onError: (Object _) {});
    notifyListeners();
  }

  Future<void> stop() async {
    await _server?.close(force: true);
    _server = null;
    notifyListeners();
  }

  Future<void> refreshIps() async {
    try {
      final list = await NetworkInterface.list(type: InternetAddressType.IPv4, includeLoopback: false);
      final found = <String>[for (final i in list) for (final a in i.addresses) a.address];
      found.sort((a, b) => (a.startsWith('192.168.') ? 0 : 1).compareTo(b.startsWith('192.168.') ? 0 : 1));
      ips = found;
    } catch (_) {
      ips = [];
    }
    notifyListeners();
  }

  // ------------------------------------------------------------------ owner actions
  void approve(String id, String perm) {
    final r = pending[id];
    if (r == null || r.status != 'pending') return;
    final tok = _token(24);
    devices[tok] = Device(
        id: _token(4),
        name: r.name,
        ip: r.ip,
        perm: permLabels.containsKey(perm) ? perm : 'read',
        approvedAt: DateTime.now(),
        lastSeen: DateTime.now());
    r.status = 'approved';
    r.token = tok;
    _saveDevices();
    notifyListeners();
  }

  void deny(String id) {
    pending[id]?.status = 'denied';
    notifyListeners();
  }

  void setPerm(String deviceId, String perm) {
    for (final d in devices.values) {
      if (d.id == deviceId && permLabels.containsKey(perm)) d.perm = perm;
    }
    _saveDevices();
    notifyListeners();
  }

  void revoke(String deviceId) {
    devices.removeWhere((_, d) => d.id == deviceId);
    _saveDevices();
    notifyListeners();
  }

  void setRoot(String path) {
    if (!Directory(path).existsSync()) throw HttpError(400, "That folder doesn't exist");
    root = p.normalize(path);
    _saveConfig();
    notifyListeners();
  }

  void setDeviceName(String name) {
    if (name.trim().isEmpty) return;
    deviceName = name.trim();
    _saveConfig();
    notifyListeners();
  }

  // ------------------------------------------------------------------ helpers
  String _ip(HttpRequest req) {
    var ip = req.connectionInfo?.remoteAddress.address ?? '';
    if (ip.startsWith('::ffff:')) ip = ip.substring(7);
    return ip;
  }

  bool _isOwner(HttpRequest req) {
    final ip = _ip(req);
    return ip.startsWith('127.') || ip == '::1' || ips.contains(ip);
  }

  Device? _device(HttpRequest req) {
    String? tok;
    for (final c in req.cookies) {
      if (c.name == cookieName) tok = c.value;
    }
    final d = tok == null ? null : devices[tok];
    if (d != null) {
      d.lastSeen = DateTime.now();
      d.ip = _ip(req);
    }
    return d;
  }

  String? _perm(HttpRequest req) => _isOwner(req) ? 'owner' : _device(req)?.perm;

  void _require(HttpRequest req, String action) {
    final pm = _perm(req);
    if (pm == null) throw HttpError(401, 'Not connected. Request access first.');
    if (!_levels[pm]!.contains(action)) throw HttpError(403, "Your access level doesn't allow this");
  }

  void _csrf(HttpRequest req) {
    if (req.headers.value('x-fb') != '1') throw HttpError(403, 'Missing request header');
  }

  String safePath(String? rel) {
    var r = (rel ?? '').replaceAll('\\', '/');
    while (r.startsWith('/')) {
      r = r.substring(1);
    }
    final base = p.normalize(root);
    final full = p.normalize(p.join(base, r));
    if (full != base && !p.isWithin(base, full)) throw HttpError(403, 'Outside the shared folder');
    return full;
  }

  Future<void> _json(HttpResponse res, Object obj, {int code = 200}) async {
    res.statusCode = code;
    res.headers.contentType = ContentType.json;
    res.headers.set('Cache-Control', 'no-store');
    res.write(jsonEncode(obj));
    await res.close();
  }

  Future<void> _html(HttpResponse res, String html, {int code = 200}) async {
    res.statusCode = code;
    res.headers.contentType = ContentType.html;
    res.headers.set('Cache-Control', 'no-store');
    res.write(html);
    await res.close();
  }

  Future<Map<String, dynamic>> _readJson(HttpRequest req) async {
    final body = await utf8.decoder.bind(req).join();
    if (body.length > (1 << 20)) throw HttpError(400, 'Request too large');
    if (body.trim().isEmpty) return {};
    try {
      final v = jsonDecode(body);
      if (v is Map<String, dynamic>) return v;
    } catch (_) {}
    throw HttpError(400, 'Bad JSON');
  }

  Future<String> _unique(String path) async {
    if (FileSystemEntity.typeSync(path) == FileSystemEntityType.notFound) return path;
    final ext = p.extension(path);
    final base = path.substring(0, path.length - ext.length);
    var i = 1;
    while (FileSystemEntity.typeSync('$base ($i)$ext') != FileSystemEntityType.notFound) {
      i++;
    }
    return '$base ($i)$ext';
  }

  // ------------------------------------------------------------------ routing
  Future<void> _handle(HttpRequest req) async {
    try {
      await _route(req);
    } on HttpError catch (e) {
      await _safeError(req.response, e.code, e.message);
    } on PathNotFoundException {
      await _safeError(req.response, 404, 'Not found');
    } on FileSystemException catch (e) {
      await _safeError(req.response, 400, e.osError?.message ?? e.message);
    } catch (e) {
      await _safeError(req.response, 500, 'Server error: $e');
    }
  }

  Future<void> _safeError(HttpResponse res, int code, String msg) async {
    try {
      await _json(res, {'error': msg}, code: code);
    } catch (_) {
      try {
        await res.close();
      } catch (_) {}
    }
  }

  Future<void> _route(HttpRequest req) async {
    final path = req.uri.path;
    final q = req.uri.queryParameters;
    final res = req.response;

    if (req.method == 'GET') {
      if (path == '/' || path == '/index.html') {
        return _html(res, _perm(req) != null ? _explorerHtml : _joinHtml);
      }
      if (path == '/admin') {
        return _html(
            res,
            '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:sans-serif;padding:40px;text-align:center">'
            'Manage access in the FileBridge app on this phone.</body>',
            code: 403);
      }
      if (path == '/favicon.ico') {
        res.statusCode = 204;
        return res.close();
      }
      if (path == '/api/info') {
        return _json(res, {'app': 'FileBridge', 'version': appVersion, 'hostname': deviceName});
      }
      if (path == '/api/whoami') {
        final pm = _perm(req);
        final d = _device(req);
        final rn = p.basename(root);
        return _json(res, {
          'perm': pm,
          'owner': pm == 'owner',
          'name': d?.name,
          'hostname': deviceName,
          'root_name': rn.isEmpty ? root : rn,
        });
      }
      if (path == '/api/request-status') {
        final r = pending[q['id'] ?? ''];
        if (r == null) return _json(res, {'status': 'unknown'});
        if (r.status == 'approved') {
          pending.remove(r.id);
          res.cookies.add(Cookie(cookieName, r.token!)
            ..path = '/'
            ..maxAge = 31536000
            ..httpOnly = true);
          notifyListeners();
          return _json(res, {'status': 'approved'});
        }
        if (r.status == 'denied') {
          pending.remove(r.id);
          notifyListeners();
        }
        return _json(res, {'status': r.status});
      }
      if (path == '/api/list') {
        _require(req, 'read');
        final dir = Directory(safePath(q['path']));
        if (!await dir.exists()) throw HttpError(404, 'Not found');
        final out = <Map<String, dynamic>>[];
        await for (final e in dir.list(followLinks: false)) {
          try {
            final st = await e.stat();
            final isDir = st.type == FileSystemEntityType.directory;
            out.add({
              'name': p.basename(e.path),
              'dir': isDir,
              'size': isDir ? 0 : st.size,
              'mtime': st.modified.millisecondsSinceEpoch / 1000,
            });
          } catch (_) {}
        }
        return _json(res, {'path': q['path'] ?? '', 'entries': out});
      }
      if (path.startsWith('/dl/')) {
        _require(req, 'read');
        return _sendFile(req, Uri.decodeComponent(path.substring(4)), q['inline'] == '1');
      }
      if (path == '/api/zip') {
        _require(req, 'read');
        return _sendZip(req, q['path']);
      }
      throw HttpError(404, 'Not found');
    }

    if (req.method == 'POST') {
      _csrf(req);
      final b = await _readJson(req);
      switch (path) {
        case '/api/request-access': {
          if (_isOwner(req)) return _json(res, {'status': 'owner'});
          var name = (_s(b['name']) ?? '').trim();
          if (name.isEmpty) name = 'Unknown device';
          if (name.length > 60) name = name.substring(0, 60);
          final ip = _ip(req);
          for (final r in pending.values) {
            if (r.ip == ip && r.status == 'pending') {
              r.name = name;
              notifyListeners();
              return _json(res, {'id': r.id});
            }
          }
          if (waiting.length >= 30) throw HttpError(403, 'Too many waiting requests');
          final id = _token(9);
          final r = AccessRequest(id: id, name: name, ip: ip, ua: req.headers.value('user-agent') ?? '');
          pending[id] = r;
          onNewRequest?.call(r);
          notifyListeners();
          return _json(res, {'id': id});
        }
        case '/api/leave': {
          String? tok;
          for (final c in req.cookies) {
            if (c.name == cookieName) tok = c.value;
          }
          if (tok != null && devices.remove(tok) != null) {
            _saveDevices();
            notifyListeners();
          }
          res.cookies.add(Cookie(cookieName, 'x')
            ..path = '/'
            ..maxAge = 0);
          return _json(res, {'ok': true});
        }
        case '/api/mkdir': {
          _require(req, 'upload');
          final dest = p.join(safePath(_s(b['path'])), cleanName(_s(b['name'])));
          if (FileSystemEntity.typeSync(dest) != FileSystemEntityType.notFound) {
            throw HttpError(409, 'Something with that name already exists');
          }
          await Directory(dest).create();
          return _json(res, {'ok': true});
        }
        case '/api/rename': {
          _require(req, 'full');
          final src = safePath(_s(b['path']));
          if (src == p.normalize(root)) throw HttpError(403, "Can't rename the shared folder");
          final dest = p.join(p.dirname(src), cleanName(_s(b['name'])));
          if (FileSystemEntity.typeSync(dest) != FileSystemEntityType.notFound) {
            throw HttpError(409, 'Something with that name already exists');
          }
          final t = FileSystemEntity.typeSync(src);
          if (t == FileSystemEntityType.directory) {
            await Directory(src).rename(dest);
          } else if (t == FileSystemEntityType.notFound) {
            throw HttpError(404, 'Not found');
          } else {
            await File(src).rename(dest);
          }
          return _json(res, {'ok': true});
        }
        case '/api/delete': {
          _require(req, 'full');
          final paths = b['paths'] is List ? (b['paths'] as List) : const [];
          for (final rel in paths) {
            final full = safePath(_s(rel));
            if (full == p.normalize(root)) throw HttpError(403, "Can't delete the shared folder");
            final t = FileSystemEntity.typeSync(full, followLinks: false);
            if (t == FileSystemEntityType.directory) {
              await Directory(full).delete(recursive: true);
            } else if (t != FileSystemEntityType.notFound) {
              await File(full).delete();
            }
          }
          return _json(res, {'ok': true});
        }
      }
      throw HttpError(404, 'Not found');
    }

    if (req.method == 'PUT' && path == '/api/upload') {
      _csrf(req);
      _require(req, 'upload');
      return _receiveUpload(req, q);
    }
    throw HttpError(404, 'Not found');
  }

  Future<void> _receiveUpload(HttpRequest req, Map<String, String> q) async {
    final folder = safePath(q['path']);
    final parts = (q['name'] ?? '')
        .replaceAll('\\', '/')
        .split('/')
        .where((x) => x.isNotEmpty)
        .map(cleanName)
        .toList();
    if (parts.isEmpty) throw HttpError(400, 'Missing file name');
    final destDir = p.joinAll([folder, ...parts.sublist(0, parts.length - 1)]);
    final base = p.normalize(root);
    if (destDir != base && !p.isWithin(base, destDir)) throw HttpError(403, 'Outside the shared folder');
    await Directory(destDir).create(recursive: true);
    final dest = await _unique(p.join(destDir, parts.last));
    final tmp = File('$dest.fbpart');
    final expected = req.contentLength;
    var got = 0;
    final sink = tmp.openWrite();
    var closed = false;
    try {
      await for (final chunk in req) {
        got += chunk.length;
        sink.add(chunk);
      }
      await sink.close();
      closed = true;
      if (expected >= 0 && got != expected) throw HttpError(400, 'Upload incomplete');
      await tmp.rename(dest);
    } finally {
      if (!closed) {
        try {
          await sink.close();
        } catch (_) {}
      }
      if (await tmp.exists()) await tmp.delete();
    }
    return _json(req.response, {'ok': true, 'name': p.basename(dest)});
  }

  Future<void> _sendFile(HttpRequest req, String rel, bool inline) async {
    final res = req.response;
    final f = File(safePath(rel));
    if (!await f.exists()) throw HttpError(404, 'Not found');
    final size = await f.length();
    var start = 0, end = size - 1, code = 200;
    final rng = req.headers.value('range') ?? '';
    if (rng.startsWith('bytes=')) {
      final spec = rng.substring(6).split(',').first.trim();
      final dash = spec.indexOf('-');
      if (dash >= 0) {
        final s = spec.substring(0, dash), e = spec.substring(dash + 1);
        final si = int.tryParse(s), ei = int.tryParse(e);
        if (s.isEmpty && ei != null) {
          start = max(0, size - ei);
          code = 206;
        } else if (si != null) {
          start = si;
          end = ei != null ? min(ei, size - 1) : size - 1;
          code = 206;
        }
        if (code == 206 && (start >= size || start > end)) {
          res.statusCode = 416;
          res.headers.set('Content-Range', 'bytes */$size');
          res.headers.contentLength = 0;
          return res.close();
        }
      }
    }
    final len = size == 0 ? 0 : end - start + 1;
    res.statusCode = code;
    res.headers.contentType = ContentType.parse(lookupMimeType(f.path) ?? 'application/octet-stream');
    res.headers.contentLength = len;
    res.headers.set('Accept-Ranges', 'bytes');
    res.headers.set('Content-Disposition',
        "${inline ? 'inline' : 'attachment'}; filename*=UTF-8''${Uri.encodeComponent(p.basename(f.path))}");
    if (code == 206) res.headers.set('Content-Range', 'bytes $start-$end/$size');
    if (len > 0) await res.addStream(f.openRead(start, end + 1));
    await res.close();
  }

  Future<void> _sendZip(HttpRequest req, String? rel) async {
    final dirPath = safePath(rel);
    final dir = Directory(dirPath);
    if (!await dir.exists()) throw HttpError(404, 'Not found');
    final bn = p.basename(dirPath);
    final name = bn.isEmpty ? 'shared' : bn;
    final files = <File>[];
    await for (final e in dir.list(recursive: true, followLinks: false).handleError((Object _) {})) {
      if (e is File) files.add(e);
    }
    final res = req.response;
    res.headers.contentType = ContentType('application', 'zip');
    res.headers.set('Content-Disposition', "attachment; filename*=UTF-8''${Uri.encodeComponent(name)}.zip");
    final zw = ZipStreamWriter(res);
    for (final f in files) {
      try {
        await zw.addFile(f, p.join(name, p.relative(f.path, from: dirPath)).replaceAll('\\', '/'));
      } catch (_) {}
    }
    zw.finish();
    await res.close();
  }
}
