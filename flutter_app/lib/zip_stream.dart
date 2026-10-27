// Minimal streaming ZIP writer (STORED, no compression) so folders can be
// downloaded as one file without extra packages. Files over 4 GB are skipped.
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

final List<int> _crcTable = List<int>.generate(256, (n) {
  var c = n;
  for (var k = 0; k < 8; k++) {
    c = (c & 1) != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
  }
  return c;
});

int crc32Update(int crc, List<int> data) {
  var c = crc ^ 0xFFFFFFFF;
  for (final b in data) {
    c = _crcTable[(c ^ b) & 0xFF] ^ (c >> 8);
  }
  return c ^ 0xFFFFFFFF;
}

int _dosTime(DateTime d) => (d.hour << 11) | (d.minute << 5) | (d.second ~/ 2);
int _dosDate(DateTime d) => ((max(d.year, 1980) - 1980) << 9) | (d.month << 5) | d.day;

class ZipStreamWriter {
  ZipStreamWriter(this.out);

  final IOSink out;
  final BytesBuilder _central = BytesBuilder();
  int _offset = 0;
  int _count = 0;

  Future<void> addFile(File file, String name) async {
    final size = await file.length();
    if (size >= 0xFFFFFFFF || _count >= 0xFFFF) return;
    final modified = await file.lastModified();
    // First pass: CRC, so the header can be written before the data.
    var crc = 0;
    await for (final chunk in file.openRead()) {
      crc = crc32Update(crc, chunk);
    }
    final nameBytes = utf8.encode(name);
    final time = _dosTime(modified), date = _dosDate(modified);

    final h = ByteData(30);
    h.setUint32(0, 0x04034b50, Endian.little);
    h.setUint16(4, 20, Endian.little);
    h.setUint16(6, 0x0800, Endian.little); // UTF-8 names
    h.setUint16(8, 0, Endian.little); // stored
    h.setUint16(10, time, Endian.little);
    h.setUint16(12, date, Endian.little);
    h.setUint32(14, crc, Endian.little);
    h.setUint32(18, size, Endian.little);
    h.setUint32(22, size, Endian.little);
    h.setUint16(26, nameBytes.length, Endian.little);
    h.setUint16(28, 0, Endian.little);
    out.add(h.buffer.asUint8List());
    out.add(nameBytes);
    var written = 0;
    await for (final chunk in file.openRead(0, size)) {
      out.add(chunk);
      written += chunk.length;
    }
    if (written != size) {
      throw const FileSystemException('File changed while zipping');
    }

    final c = ByteData(46);
    c.setUint32(0, 0x02014b50, Endian.little);
    c.setUint16(4, 20, Endian.little);
    c.setUint16(6, 20, Endian.little);
    c.setUint16(8, 0x0800, Endian.little);
    c.setUint16(10, 0, Endian.little);
    c.setUint16(12, time, Endian.little);
    c.setUint16(14, date, Endian.little);
    c.setUint32(16, crc, Endian.little);
    c.setUint32(20, size, Endian.little);
    c.setUint32(24, size, Endian.little);
    c.setUint16(28, nameBytes.length, Endian.little);
    c.setUint16(30, 0, Endian.little);
    c.setUint16(32, 0, Endian.little);
    c.setUint16(34, 0, Endian.little);
    c.setUint16(36, 0, Endian.little);
    c.setUint32(38, 0, Endian.little);
    c.setUint32(42, _offset, Endian.little);
    _central.add(c.buffer.asUint8List());
    _central.add(nameBytes);

    _offset += 30 + nameBytes.length + size;
    _count++;
  }

  void finish() {
    final cd = _central.takeBytes();
    out.add(cd);
    final e = ByteData(22);
    e.setUint32(0, 0x06054b50, Endian.little);
    e.setUint16(4, 0, Endian.little);
    e.setUint16(6, 0, Endian.little);
    e.setUint16(8, _count, Endian.little);
    e.setUint16(10, _count, Endian.little);
    e.setUint32(12, cd.length, Endian.little);
    e.setUint32(16, _offset, Endian.little);
    e.setUint16(20, 0, Endian.little);
    out.add(e.buffer.asUint8List());
  }
}
