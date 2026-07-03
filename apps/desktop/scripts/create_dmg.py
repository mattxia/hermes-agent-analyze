#!/usr/bin/env python3
"""
Create a macOS DMG installer from a .app bundle on Windows.

This script creates a DMG by:
1. Building an ISO 9660 image containing the .app bundle
2. Wrapping it in Apple's UDIF (Universal Disk Image Format) container

macOS can mount ISO 9660 DMGs, so the resulting .dmg is usable on macOS.
Note: file permissions (execute bits) are not preserved in ISO 9660.
Run `chmod +x` on the executable after extracting on macOS.
"""

import os
import sys
import struct
import zlib
import plistlib
import base64
import hashlib
import io

def _to_iso_name(name):
    """Convert a filename to a valid ISO 9660 name (uppercase A-Z, 0-9, _)."""
    return ''.join(c if (c.isascii() and c.isalnum()) else '_' for c in name.upper())


def create_iso_image(app_path, iso_path):
    """Create an ISO 9660 image with the .app bundle using pycdlib."""
    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=True, rock_ridge='1.09')

    # Add the .app directory to the root of the ISO
    iso.add_directory('/HERMES_APP', rr_name='Hermes.app', joliet_path='/Hermes.app')

    # Track used ISO names to avoid collisions
    used_names = set()

    def make_iso_path(prefix, name):
        """Build a unique ISO 9660 path from prefix and name."""
        base = _to_iso_name(name)
        candidate = f'{prefix}/{base}'
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        # Append a number for uniqueness
        i = 1
        while f'{prefix}/{base}_{i}' in used_names:
            i += 1
        candidate = f'{prefix}/{base}_{i}'
        used_names.add(candidate)
        return candidate

    # Recursively add all files and directories
    file_count = 0
    for root, dirs, files in os.walk(app_path):
        rel_root = os.path.relpath(root, app_path)
        if rel_root == '.':
            iso_prefix = '/HERMES_APP'
            joliet_prefix = '/Hermes.app'
        else:
            rel_root_unix = rel_root.replace('\\', '/')
            # Build ISO prefix by converting each path component
            iso_parts = [_to_iso_name(p) for p in rel_root_unix.split('/')]
            iso_prefix = '/HERMES_APP/' + '/'.join(iso_parts)
            joliet_prefix = '/Hermes.app/' + rel_root_unix

        # Add subdirectories
        for d in dirs:
            iso_dir = make_iso_path(iso_prefix, d)
            joliet_dir = joliet_prefix + '/' + d
            try:
                iso.add_directory(iso_dir, rr_name=d, joliet_path=joliet_dir)
            except Exception:
                pass  # Directory might already exist

        # Add files
        for f in files:
            file_path = os.path.join(root, f)
            iso_file = make_iso_path(iso_prefix, f)
            joliet_file = joliet_prefix + '/' + f
            try:
                iso.add_file(file_path, iso_file, rr_name=f, joliet_path=joliet_file)
                file_count += 1
            except Exception as e:
                print(f'  WARNING: could not add {file_path}: {e}')

    print(f'  Added {file_count} files to ISO')

    # Write the ISO image
    iso.write(iso_path)
    iso.close()

    return os.path.getsize(iso_path)


def create_mish_block(data_size, sector_size=512):
    """Create a mish (BLKX) block for uncompressed data."""
    sector_count = data_size // sector_size

    # CRC32 checksum of the data
    # Note: we calculate this separately since we stream the data

    # Mish header (packed, big-endian)
    header = struct.pack('>IIIQQQIIIIIIII',
        0x6D697368,    # signature: 'mish'
        1,             # version
        0,             # type
        0,             # sector_number (first sector)
        sector_count,  # sector_count
        0,             # data_offset (offset in data fork)
        0,             # buffer_size
        2,             # block_entry_count (data + terminator)
        0, 0, 0, 0, 0, 0  # reserved1-6
    )

    # Checksum placeholder (32 bytes) - will be filled with CRC32
    # Format: uint32 type (2=CRC32), uint32 size (4), uint32[6] checksum data
    # Actually, the checksum in the mish header is just 32 bytes of checksum data
    # The type and size are in separate fields in some implementations
    # Let me use the format from libdmg: just 32 bytes of raw checksum data
    checksum = b'\x00' * 32  # Will be filled later

    # Block entry 1: uncompressed data
    entry1 = struct.pack('>IIQQQ',
        0,             # type: uncompressed
        0,             # reserved
        sector_count,  # sector_count
        0,             # compressed_length (0 for uncompressed)
        0              # uncompressed_length (0 for uncompressed)
    )

    # Block entry 2: terminator
    entry2 = struct.pack('>IIQQQ',
        0x7FFFFFFF,    # type: terminator
        0,             # reserved
        0,             # sector_count
        0,             # compressed_length
        0              # uncompressed_length
    )

    return header + checksum + entry1 + entry2


def calculate_crc32(data):
    """Calculate CRC32 checksum."""
    return zlib.crc32(data) & 0xFFFFFFFF


def create_dmg(iso_path, dmg_path, volume_name='Install Hermes'):
    """Create a UDIF DMG file from an ISO image."""
    sector_size = 512

    # Read the ISO image
    with open(iso_path, 'rb') as f:
        iso_data = f.read()

    # Pad to sector boundary
    if len(iso_data) % sector_size != 0:
        padding = sector_size - (len(iso_data) % sector_size)
        iso_data += b'\x00' * padding

    data_fork_length = len(iso_data)
    sector_count = data_fork_length // sector_size

    # Calculate CRC32 of the data fork
    data_crc = calculate_crc32(iso_data)

    print(f'  ISO size: {data_fork_length} bytes ({sector_count} sectors)')
    print(f'  CRC32: {data_crc:#010x}')

    # Create mish block
    mish_data = create_mish_block(data_fork_length, sector_size)

    # Fill in the checksum in the mish block
    # The checksum is at offset 68 (after the header fields)
    mish_data = mish_data[:68] + struct.pack('>II', data_crc, 0) + mish_data[76:]

    # Create XML plist
    mish_b64 = base64.b64encode(mish_data)

    plist_dict = {
        'resource-fork': {
            'blkx': [
                {
                    'Attributes': '0x0050',
                    'CFName': 'Driver Descriptor Map (DDM : 0)',
                    'Data': mish_b64,
                    'ID': '-1',
                    'Name': 'Driver Descriptor Map (DDM : 0)',
                }
            ]
        }
    }

    xml_bytes = plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)

    # Calculate offsets
    data_fork_offset = 0
    xml_offset = data_fork_length
    xml_length = len(xml_bytes)

    # Create koly header (512 bytes, big-endian)
    # Format: 4s I I I Q Q Q Q Q I I 16s I I 128s Q Q 120s I I 128s I Q I I I
    data_checksum = struct.pack('>II', data_crc, 0) + b'\x00' * 120  # 128 bytes
    master_checksum = struct.pack('>II', data_crc, 0) + b'\x00' * 120  # 128 bytes

    koly = struct.pack('>4sIIIQQQQQII16sII',
        b'koly',           # fMagic
        4,                 # fVersion
        512,               # fHeaderSize
        1,                 # fFlags (read-only)
        0,                 # fRunningDataForkOffset
        data_fork_offset,  # fDataForkOffset
        data_fork_length,  # fDataForkLength
        0,                 # fRsrcForkOffset
        0,                 # fRsrcForkLength
        0,                 # fSegmentNumber
        1,                 # fSegmentCount
        b'\x00' * 16,      # fSegmentID
        2,                 # fDataChecksumType (CRC32)
        4,                 # fDataChecksumSize
    )
    koly += data_checksum                    # fDataChecksum (128 bytes)
    koly += struct.pack('>QQ', xml_offset, xml_length)  # fXMLOffset, fXMLLength
    koly += b'\x00' * 120                    # fReserved1 (120 bytes)
    koly += struct.pack('>II', 2, 4)         # fChecksumType (CRC32), fChecksumSize
    koly += master_checksum                  # fChecksum (128 bytes)
    koly += struct.pack('>IQIII',
        0,                  # fImageVariant
        sector_count,       # fSectorCount
        0,                  # fReserved2
        0,                  # fReserved3
        0,                  # fReserved4
    )

    assert len(koly) == 512, f'koly header size is {len(koly)}, expected 512'

    # Write DMG file
    with open(dmg_path, 'wb') as f:
        f.write(iso_data)    # data fork (raw ISO image)
        f.write(xml_bytes)   # XML plist
        f.write(koly)        # koly header

    return os.path.getsize(dmg_path)


def main():
    desktop_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_path = os.path.join(desktop_root, 'release', 'mac', 'Hermes.app')
    iso_path = os.path.join(desktop_root, 'release', 'Hermes-temp.iso')
    dmg_path = os.path.join(desktop_root, 'release', 'Hermes-0.17.0-mac-x64.dmg')

    if not os.path.exists(app_path):
        print(f'ERROR: {app_path} not found. Run the electron-builder first.')
        sys.exit(1)

    print(f'Creating ISO image from {app_path}...')
    iso_size = create_iso_image(app_path, iso_path)
    print(f'  ISO created: {iso_size} bytes')

    print(f'Creating DMG...')
    dmg_size = create_dmg(iso_path, dmg_path)
    print(f'  DMG created: {dmg_size} bytes')
    print(f'  Output: {dmg_path}')

    # Clean up temp ISO
    try:
        os.remove(iso_path)
        print(f'  Cleaned up temp ISO')
    except:
        pass

    print('Done!')


if __name__ == '__main__':
    main()
