# Third-party notices

The Python sources are built with these upstream components. When distributing a binary, retain the exact dependency versions, include their applicable notices and license texts with the release archive, and record the archive's SHA-256. The portable application itself is a single EXE.

| Component | Where used | License information |
| --- | --- | --- |
| CPython | Embedded Python runtime. The version is the interpreter used for the build. | [Python Software Foundation and bundled notices](https://docs.python.org/3/license.html) |
| python-zstandard 0.25.0 | Reads compressed DeepSeek task journals. The wheel supplies notices for its bundled code. | [python-zstandard license](https://github.com/indygreg/python-zstandard/blob/0.25.0/LICENSE) |
| Nuitka 4.3.5 | Builds the single-file EXE and supplies bundled runtime code. | [Nuitka source and notices](https://github.com/Nuitka/Nuitka/tree/4.3.5) · [Runtime exception](https://github.com/Nuitka/Nuitka/blob/4.3.5/LICENSE-RUNTIME.txt) |
| pefile 2024.8.26 | Build-time PE inspection. | [pefile source and license](https://github.com/erocarrera/pefile/tree/v2024.8.26) |

The optional Node.js journal reader uses a separately installed Node.js runtime when explicitly requested. Check the actual dependency list for each supported build environment before releasing a binary.
