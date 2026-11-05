# 라이선스 원문 틀 — 지원하는 허용형 라이선스(MIT·ISC·BSD-2/3·Apache-2.0)의 정규화된 전체 본문 틀과, 부적격 판정에만 쓰는 다른 라이선스의 제목 문구를 둔다.
"""Normalized license texts the strict matcher compares a whole file with. `{NAME}` marks a variable part.

Normalization (see `license.normalize`): typographic quotes and dashes become ASCII, letters are lower-cased and
every run of whitespace becomes one space. The BSD templates also drop list markers and read "copyright owner" as
"copyright holder", their two common spellings. The checked-in templates were compared with real license files
(six MIT, two Apache-2.0, three BSD) before use; tests pin them.

Apache-2.0 is long, so its terms (from "apache license version 2.0, january 2004" through "end of terms and
conditions") are pinned by the sha256 of their normalized text, with `https://www.apache.org/` read as `http://`.
The appendix after the terms is optional and its copyright line is free.
"""
from __future__ import annotations

MIT_BODY = (
    'permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated '
    'documentation files (the "software"), to deal in the software without restriction, including without limitation '
    'the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, and '
    'to permit persons to whom the software is furnished to do so, subject to the following conditions: the above '
    'copyright notice and this permission notice shall be included in all copies or substantial portions of the '
    'software. the software is provided "as is", without warranty of any kind, express or implied, including but not '
    'limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. in no event '
    'shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action '
    'of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other '
    'dealings in the software.')

BSD_OPENING = ('redistribution and use in source and binary forms, with or without modification, are permitted '
               'provided that the following conditions are met: redistributions of source code must retain the above '
               'copyright notice, this list of conditions and the following disclaimer. redistributions in binary '
               'form must reproduce the above copyright notice, this list of conditions and the following disclaimer '
               'in the documentation and/or other materials provided with the distribution.')
BSD_THIRD = ('neither the name of {HOLDER} nor the names of {ITS} contributors may be used to '
             'endorse or promote products derived from this software without specific prior written permission.')
BSD_DISCLAIMER = (
    'this software is provided by the copyright holders and contributors "as is" and any express or implied '
    'warranties, including, but not limited to, the implied warranties of merchantability and fitness for a '
    'particular purpose are disclaimed. in no event shall the copyright holder or contributors be liable for any '
    'direct, indirect, incidental, special, exemplary, or consequential damages (including, but not limited to, '
    'procurement of substitute goods or services; loss of use, data, or profits; or business interruption) however '
    'caused and on any theory of liability, whether in contract, strict liability, or tort (including negligence or '
    'otherwise) arising in any way out of the use of this software, even if advised of the possibility of such '
    'damage.')

ISC_BODY = (
    'permission to use, copy, modify, {AND} distribute this software for any purpose with or without fee '
    'is hereby granted, provided that the above copyright notice and this permission notice appear in all copies. '
    'the software is provided "as is" and the {DISCLAIM} all warranties with regard to '
    'this software including all implied warranties of merchantability and fitness. in no event shall the {AUTHOR} '
    'be liable for any special, direct, indirect, or consequential damages or any damages whatsoever resulting from '
    'loss of use, data or profits, whether in an action of contract, negligence or other tortious action, arising '
    'out of or in connection with the use or performance of this software.')

APACHE_START = "apache license version 2.0, january 2004"
APACHE_END = "end of terms and conditions"
APACHE_TERMS_SHA256 = "95cef6332b35354c12f9d666ab9ff47002e6f7ef937924896882b2e0cdb7a0d6"
APACHE_APPENDIX = (
    'appendix: how to apply the apache license to your work. to apply the apache license to your work, attach the '
    'following boilerplate notice, with the fields enclosed by brackets "[]" replaced with your own identifying '
    'information. (don\'t include the brackets!) the text should be enclosed in the appropriate comment syntax for '
    'the file format. we also recommend that a file or class name and description of purpose be included on the '
    'same "printed page" as the copyright notice for easier identification within third-party archives. '
    '{COPYRIGHT} licensed under the apache license, version 2.0 (the "license"); you may not use this file '
    'except in compliance with the license. you may obtain a copy of the license at '
    'http://www.apache.org/licenses/license-2.0 unless required by applicable law or agreed to in writing, software '
    'distributed under the license is distributed on an "as is" basis, without warranties or conditions of any kind, '
    'either express or implied. see the license for the specific language governing permissions and limitations '
    'under the license.')

# Titles that name a license this kit never treats as eligible; enough to say "excluded", never "eligible".
OTHER_TITLES = (
    ("AGPL-3.0", ("gnu affero general public license", "version 3")),
    ("LGPL-3.0", ("gnu lesser general public license", "version 3")),
    ("LGPL-2.1", ("gnu lesser general public license", "version 2.1")),
    ("GPL-3.0", ("gnu general public license", "version 3")),
    ("GPL-2.0", ("gnu general public license", "version 2")),
    ("MPL-2.0", ("mozilla public license", "2.0")),
    ("Unlicense", ("this is free and unencumbered software released into the public domain",)),
    ("BUSL-1.1", ("business source license",)),
    ("SSPL-1.0", ("server side public license",)),
)
