import 'dart:async';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:url_launcher/url_launcher.dart';

import 'server.dart';

const _seed = Color(0xFF6B8BFF);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const FileBridgeApp());
}

class FileBridgeApp extends StatelessWidget {
  const FileBridgeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'FileBridge',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: _seed, useMaterial3: true, brightness: Brightness.light),
      darkTheme: ThemeData(colorSchemeSeed: _seed, useMaterial3: true, brightness: Brightness.dark),
      themeMode: ThemeMode.system,
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
  final server = FileBridgeServer('/storage/emulated/0/Download');
  final peerCtl = TextEditingController();
  final Map<String, String> chosenPerm = {};
  bool ready = false;
  bool storageOk = false;
  String? error;
  Timer? tick;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    server.addListener(_changed);
    server.onNewRequest = (r) {
      HapticFeedback.mediumImpact();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${r.name} wants to connect')));
      }
    };
    tick = Timer.periodic(const Duration(seconds: 5), (_) => _changed());
    _boot();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    tick?.cancel();
    server.removeListener(_changed);
    server.stop();
    peerCtl.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      server.refreshIps();
      _checkStorage();
    }
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _boot() async {
    try {
      await _askStorage();
      await server.init();
      await server.start();
    } catch (e) {
      error = '$e';
    }
    if (mounted) setState(() => ready = true);
  }

  Future<void> _checkStorage() async {
    final ok = await Permission.manageExternalStorage.isGranted || await Permission.storage.isGranted;
    if (mounted && ok != storageOk) setState(() => storageOk = ok);
  }

  Future<void> _askStorage() async {
    var ok = await Permission.manageExternalStorage.isGranted;
    if (!ok) ok = (await Permission.manageExternalStorage.request()).isGranted;
    if (!ok) ok = (await Permission.storage.request()).isGranted;
    if (mounted) setState(() => storageOk = ok);
  }

  Future<void> _toggle(bool on) async {
    try {
      if (on) {
        await server.start();
      } else {
        await server.stop();
      }
    } catch (e) {
      _snack('$e');
    }
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  Future<void> _pickFolder() async {
    try {
      final path = await FilePicker.getDirectoryPath();
      if (path == null) return;
      server.setRoot(path);
      _snack('Now sharing $path');
    } catch (e) {
      _snack('$e');
    }
  }

  Future<void> _typeFolder() async {
    final ctl = TextEditingController(text: server.root);
    final v = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Folder path'),
        content: TextField(controller: ctl, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, ctl.text.trim()), child: const Text('Share')),
        ],
      ),
    );
    if (v == null || v.isEmpty) return;
    try {
      server.setRoot(v);
    } catch (e) {
      _snack('$e');
    }
  }

  Future<void> _rename() async {
    final ctl = TextEditingController(text: server.deviceName);
    final v = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Device name'),
        content: TextField(
            controller: ctl,
            autofocus: true,
            decoration: const InputDecoration(helperText: 'Shown to people who connect')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, ctl.text), child: const Text('Save')),
        ],
      ),
    );
    if (v != null) server.setDeviceName(v);
  }

  Future<void> _openPeer() async {
    var v = peerCtl.text.trim();
    if (v.isEmpty) return;
    if (!v.startsWith('http://') && !v.startsWith('https://')) v = 'http://$v';
    final hostPart = v.replaceFirst(RegExp(r'^https?://'), '');
    if (!RegExp(r':\d+(/|$)').hasMatch(hostPart)) v = '${v.replaceFirst(RegExp(r'/$'), '')}:8000';
    final ok = await launchUrl(Uri.parse(v), mode: LaunchMode.externalApplication);
    if (!ok) _snack('Could not open $v');
  }

  String _ago(DateTime t) {
    final s = DateTime.now().difference(t).inSeconds;
    if (s < 60) return 'just now';
    if (s < 3600) return '${s ~/ 60} min ago';
    if (s < 86400) return '${s ~/ 3600} h ago';
    return '${s ~/ 86400} d ago';
  }

  String _os(String ua) {
    if (ua.contains('Android')) return 'Android';
    if (ua.contains('iPhone') || ua.contains('iPad')) return 'iOS';
    if (ua.contains('Windows')) return 'Windows';
    if (ua.contains('Mac OS')) return 'macOS';
    if (ua.contains('Linux')) return 'Linux';
    return 'Browser';
  }

  Widget _card({required Widget child, Color? color}) => Card(
        elevation: 0,
        color: color,
        margin: const EdgeInsets.only(bottom: 14),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: Theme.of(context).colorScheme.outlineVariant)),
        child: Padding(padding: const EdgeInsets.all(16), child: child),
      );

  Widget _title(String t, [String? sub]) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(t, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
          if (sub != null)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(sub, style: Theme.of(context).textTheme.bodySmall),
            ),
        ]),
      );

  Widget _avatar(String name, {bool? online}) {
    final cs = Theme.of(context).colorScheme;
    return Stack(children: [
      CircleAvatar(
        backgroundColor: cs.primaryContainer,
        child: Text(name.isEmpty ? '?' : name.characters.first.toUpperCase(),
            style: TextStyle(color: cs.onPrimaryContainer, fontWeight: FontWeight.bold)),
      ),
      if (online != null)
        Positioned(
          right: 0,
          bottom: 0,
          child: Container(
            width: 12,
            height: 12,
            decoration: BoxDecoration(
              color: online ? Colors.green : Colors.grey,
              shape: BoxShape.circle,
              border: Border.all(color: cs.surface, width: 2),
            ),
          ),
        ),
    ]);
  }

  Widget _connectCard() {
    final cs = Theme.of(context).colorScheme;
    if (!server.running) {
      return _card(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _title('Sharing is off', 'Nobody can reach your files right now.'),
          FilledButton.icon(
              onPressed: () => _toggle(true), icon: const Icon(Icons.play_arrow), label: const Text('Start sharing')),
        ]),
      );
    }
    final urls = server.urls;
    return _card(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        _title('Connect from another device', 'Same Wi-Fi or hotspot. Open this address in any browser, or scan the code.'),
        if (urls.isEmpty)
          Text('Not connected to a network. Turn on Wi-Fi or your hotspot.', style: TextStyle(color: cs.error)),
        for (final u in urls)
          Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.fromLTRB(14, 6, 6, 6),
            decoration: BoxDecoration(color: cs.surfaceContainerHighest, borderRadius: BorderRadius.circular(12)),
            child: Row(children: [
              Expanded(
                child: SelectableText(u,
                    style: TextStyle(
                        fontFamily: 'monospace', fontSize: 19, fontWeight: FontWeight.w700, color: cs.primary)),
              ),
              IconButton(
                tooltip: 'Copy',
                icon: const Icon(Icons.copy),
                onPressed: () {
                  Clipboard.setData(ClipboardData(text: u));
                  _snack('Copied $u');
                },
              ),
            ]),
          ),
        if (urls.isNotEmpty)
          Center(
            child: Container(
              margin: const EdgeInsets.only(top: 6),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(12)),
              child: QrImageView(data: urls.first, size: 170, backgroundColor: Colors.white),
            ),
          ),
        const SizedBox(height: 8),
        Text('Device name: ${server.deviceName} · port ${server.port}', style: Theme.of(context).textTheme.bodySmall),
      ]),
    );
  }

  Widget _folderCard() => _card(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _title('Shared folder', 'Approved devices only see what is inside this folder.'),
          Row(children: [
            const Icon(Icons.folder, color: Color(0xFFF3B43F)),
            const SizedBox(width: 8),
            Expanded(child: Text(server.root, style: const TextStyle(fontWeight: FontWeight.w500))),
          ]),
          const SizedBox(height: 10),
          Wrap(spacing: 8, children: [
            FilledButton.tonalIcon(
                onPressed: _pickFolder, icon: const Icon(Icons.folder_open), label: const Text('Change folder')),
            TextButton(onPressed: _typeFolder, child: const Text('Type a path')),
          ]),
        ]),
      );

  Widget _requestsCard() {
    final reqs = server.waiting;
    if (reqs.isEmpty) return const SizedBox.shrink();
    final cs = Theme.of(context).colorScheme;
    return _card(
      color: cs.tertiaryContainer.withValues(alpha: 0.45),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        _title('Access requests (${reqs.length})', 'Choose what each device may do, then approve.'),
        for (final r in reqs)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                _avatar(r.name),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(r.name, style: const TextStyle(fontWeight: FontWeight.w600)),
                    Text('${r.ip} · ${_os(r.ua)} · asked ${_ago(r.time)}', style: Theme.of(context).textTheme.bodySmall),
                  ]),
                ),
              ]),
              const SizedBox(height: 6),
              Wrap(spacing: 8, crossAxisAlignment: WrapCrossAlignment.center, children: [
                DropdownButton<String>(
                  value: chosenPerm[r.id] ?? 'read',
                  items: [
                    for (final e in permLabels.entries) DropdownMenuItem(value: e.key, child: Text(e.value)),
                  ],
                  onChanged: (v) => setState(() => chosenPerm[r.id] = v ?? 'read'),
                ),
                FilledButton(
                    onPressed: () => server.approve(r.id, chosenPerm[r.id] ?? 'read'), child: const Text('Approve')),
                TextButton(
                  onPressed: () => server.deny(r.id),
                  style: TextButton.styleFrom(foregroundColor: cs.error),
                  child: const Text('Deny'),
                ),
              ]),
            ]),
          ),
      ]),
    );
  }

  Widget _devicesCard() {
    final devs = server.devices.values.toList()..sort((a, b) => b.lastSeen.compareTo(a.lastSeen));
    return _card(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        _title('Approved devices', 'Change access or remove a device at any time.'),
        if (devs.isEmpty)
          Text('No devices yet. Share your address and approve requests as they come in.',
              style: Theme.of(context).textTheme.bodyMedium),
        for (final d in devs)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: _avatar(d.name, online: d.online),
            title: Text(d.name),
            subtitle: Text('${d.ip} · ${d.online ? 'online now' : 'last seen ${_ago(d.lastSeen)}'}'),
            trailing: PopupMenuButton<String>(
              tooltip: 'Access',
              onSelected: (v) async {
                if (v != 'remove') {
                  server.setPerm(d.id, v);
                  return;
                }
                final yes = await showDialog<bool>(
                  context: context,
                  builder: (c) => AlertDialog(
                    title: Text('Remove ${d.name}?'),
                    content: const Text('It loses access immediately and must request again.'),
                    actions: [
                      TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
                      FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Remove')),
                    ],
                  ),
                );
                if (yes == true) server.revoke(d.id);
              },
              itemBuilder: (_) => [
                for (final e in permLabels.entries)
                  CheckedPopupMenuItem(value: e.key, checked: d.perm == e.key, child: Text(e.value)),
                const PopupMenuDivider(),
                const PopupMenuItem(value: 'remove', child: Text('Remove device')),
              ],
              child: Chip(label: Text(permLabels[d.perm] ?? d.perm)),
            ),
          ),
      ]),
    );
  }

  Widget _peerCard() => _card(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _title("Open someone else's files", 'If a colleague runs FileBridge too, enter their address.'),
          Row(children: [
            Expanded(
              child: TextField(
                controller: peerCtl,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(hintText: '192.168.1.20:8000', border: OutlineInputBorder(), isDense: true),
                onSubmitted: (_) => _openPeer(),
              ),
            ),
            const SizedBox(width: 8),
            FilledButton(onPressed: _openPeer, child: const Text('Open')),
          ]),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        title: Row(children: [
          Container(
            width: 30,
            height: 30,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(9),
              gradient: const LinearGradient(colors: [Color(0xFF6B8BFF), Color(0xFFA66BFF)]),
            ),
            child: const Icon(Icons.swap_horiz, color: Colors.white, size: 20),
          ),
          const SizedBox(width: 10),
          const Text('FileBridge', style: TextStyle(fontWeight: FontWeight.w700)),
        ]),
        actions: [
          IconButton(tooltip: 'Device name', onPressed: _rename, icon: const Icon(Icons.edit_outlined)),
          Switch(value: server.running, onChanged: ready ? _toggle : null),
          const SizedBox(width: 8),
        ],
      ),
      body: !ready
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: server.refreshIps,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (error != null)
                    _card(color: cs.errorContainer, child: Text(error!, style: TextStyle(color: cs.onErrorContainer))),
                  if (!storageOk)
                    _card(
                      color: cs.errorContainer,
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        _title('File access needed', 'Allow FileBridge to read and write your files so others can browse them.'),
                        Wrap(spacing: 8, children: [
                          FilledButton(onPressed: _askStorage, child: const Text('Allow access')),
                          TextButton(onPressed: openAppSettings, child: const Text('Open settings')),
                        ]),
                      ]),
                    ),
                  _requestsCard(),
                  _connectCard(),
                  _folderCard(),
                  _devicesCard(),
                  _peerCard(),
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Text(
                      'Keep FileBridge open while sharing. Android may pause it in the background. v$appVersion',
                      textAlign: TextAlign.center,
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                ],
              ),
            ),
    );
  }
}
