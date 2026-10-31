"""The ESP32 dash's built-in 5x7 text font (Adafruit GFX "classic" font,
glcdfont.c), printable ASCII only, for the Dashboard tab's preview.

Each character is 5 column bytes; bit 0 is the top row.

Copied from the Adafruit GFX Library, under its BSD licence:

    Software License Agreement (BSD License)

    Copyright (c) 2012 Adafruit Industries.  All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    - Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    - Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
    ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
    LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
    CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.
"""

FIRST_CHAR = 32

GLYPHS = bytes.fromhex(
    "0000000000"  # ' '
    "00005f0000"  # '!'
    "0007000700"  # '"'
    "147f147f14"  # '#'
    "242a7f2a12"  # '$'
    "2313086462"  # '%'
    "3649562050"  # '&'
    "0008070300"  # "'"
    "001c224100"  # '('
    "0041221c00"  # ')'
    "2a1c7f1c2a"  # '*'
    "08083e0808"  # '+'
    "0080703000"  # ','
    "0808080808"  # '-'
    "0000606000"  # '.'
    "2010080402"  # '/'
    "3e5149453e"  # '0'
    "00427f4000"  # '1'
    "7249494946"  # '2'
    "2141494d33"  # '3'
    "1814127f10"  # '4'
    "2745454539"  # '5'
    "3c4a494931"  # '6'
    "4121110907"  # '7'
    "3649494936"  # '8'
    "464949291e"  # '9'
    "0000140000"  # ':'
    "0040340000"  # ';'
    "0008142241"  # '<'
    "1414141414"  # '='
    "0041221408"  # '>'
    "0201590906"  # '?'
    "3e415d594e"  # '@'
    "7c1211127c"  # 'A'
    "7f49494936"  # 'B'
    "3e41414122"  # 'C'
    "7f4141413e"  # 'D'
    "7f49494941"  # 'E'
    "7f09090901"  # 'F'
    "3e41415173"  # 'G'
    "7f0808087f"  # 'H'
    "00417f4100"  # 'I'
    "2040413f01"  # 'J'
    "7f08142241"  # 'K'
    "7f40404040"  # 'L'
    "7f021c027f"  # 'M'
    "7f0408107f"  # 'N'
    "3e4141413e"  # 'O'
    "7f09090906"  # 'P'
    "3e4151215e"  # 'Q'
    "7f09192946"  # 'R'
    "2649494932"  # 'S'
    "03017f0103"  # 'T'
    "3f4040403f"  # 'U'
    "1f2040201f"  # 'V'
    "3f4038403f"  # 'W'
    "6314081463"  # 'X'
    "0304780403"  # 'Y'
    "6159494d43"  # 'Z'
    "007f414141"  # '['
    "0204081020"  # backslash
    "004141417f"  # ']'
    "0402010204"  # '^'
    "4040404040"  # '_'
    "0003070800"  # '`'
    "2054547840"  # 'a'
    "7f28444438"  # 'b'
    "3844444428"  # 'c'
    "384444287f"  # 'd'
    "3854545418"  # 'e'
    "00087e0902"  # 'f'
    "18a4a49c78"  # 'g'
    "7f08040478"  # 'h'
    "00447d4000"  # 'i'
    "2040403d00"  # 'j'
    "7f10284400"  # 'k'
    "00417f4000"  # 'l'
    "7c04780478"  # 'm'
    "7c08040478"  # 'n'
    "3844444438"  # 'o'
    "fc18242418"  # 'p'
    "18242418fc"  # 'q'
    "7c08040408"  # 'r'
    "4854545424"  # 's'
    "04043f4424"  # 't'
    "3c4040207c"  # 'u'
    "1c2040201c"  # 'v'
    "3c4030403c"  # 'w'
    "4428102844"  # 'x'
    "4c9090907c"  # 'y'
    "4464544c44"  # 'z'
    "0008364100"  # '{'
    "0000770000"  # '|'
    "0041360800"  # '}'
    "0201020402"  # '~'
)
