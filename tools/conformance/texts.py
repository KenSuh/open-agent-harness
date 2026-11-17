# 적합성 픽스처가 입력으로 쓰는 공개 라이선스 전문(MIT·ISC·BSD·GPL 제목)과 Apache 전문을 태그에서 읽는 함수.
"""Public license texts used as fixture inputs."""
from __future__ import annotations

import subprocess

MIT = ("MIT License\n\nCopyright (c) 2026 Acme\n\n"
       "Permission is hereby granted, free of charge, to any person obtaining a copy\nof this software and "
       "associated documentation files (the \"Software\"), to deal\nin the Software without restriction, "
       "including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, "
       "and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, "
       "subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be "
       "included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", "
       "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF "
       "MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS "
       "OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF "
       "CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER "
       "DEALINGS IN THE\nSOFTWARE.\n")

ISC = ("ISC License\n\nCopyright (c) 2026 Acme\n\n"
       "Permission to use, copy, modify, and/or distribute this software for any\npurpose with or without fee is "
       "hereby granted, provided that the above\ncopyright notice and this permission notice appear in all copies.\n\n"
       "THE SOFTWARE IS PROVIDED \"AS IS\" AND THE AUTHOR DISCLAIMS ALL WARRANTIES\nWITH REGARD TO THIS SOFTWARE "
       "INCLUDING ALL IMPLIED WARRANTIES OF\nMERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR\n"
       "ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES\nWHATSOEVER RESULTING FROM LOSS OF USE, "
       "DATA OR PROFITS, WHETHER IN AN\nACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF\n"
       "OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.\n")

GPL = ("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n\n"
       "Everyone is permitted to copy and distribute verbatim copies of this license document.\n")

_BSD_HEAD = ("Copyright (c) 2026 Acme\nAll rights reserved.\n\n"
             "Redistribution and use in source and binary forms, with or without\nmodification, are permitted "
             "provided that the following conditions are met:\n\n"
             "1. Redistributions of source code must retain the above copyright notice, this\n   list of conditions "
             "and the following disclaimer.\n\n"
             "2. Redistributions in binary form must reproduce the above copyright notice,\n   this list of "
             "conditions and the following disclaimer in the documentation\n   and/or other materials provided with "
             "the distribution.\n\n")
_BSD_THIRD = ("3. Neither the name of the copyright holder nor the names of its\n   contributors may be used to endorse "
              "or promote products derived from\n   this software without specific prior written permission.\n\n")
_BSD_TAIL = ("THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS \"AS IS\"\nAND ANY EXPRESS OR IMPLIED "
             "WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE\nIMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A "
             "PARTICULAR PURPOSE ARE\nDISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE\n"
             "FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL\nDAMAGES (INCLUDING, BUT NOT "
             "LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR\nSERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS "
             "INTERRUPTION) HOWEVER\nCAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,\n"
             "OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE\nOF THIS SOFTWARE, EVEN IF "
             "ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.\n")
BSD2 = "BSD 2-Clause License\n\n" + _BSD_HEAD + _BSD_TAIL
BSD3 = "BSD 3-Clause License\n\n" + _BSD_HEAD + _BSD_THIRD + _BSD_TAIL


def apache(root) -> str:
    """The Apache-2.0 terms (through END OF TERMS AND CONDITIONS) as tag v0.4.0 ships them in skill-creator."""
    full = subprocess.run(["git", "-C", str(root), "show", "v0.4.0:skills/skill-creator/LICENSE.txt"],
                          capture_output=True, check=True).stdout.decode("utf-8").replace("\r\n", "\n")
    end = "END OF TERMS AND CONDITIONS"
    return full[:full.index(end) + len(end)] + "\n"
