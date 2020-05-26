# Copyright 2020 BlueFog Team. All Rights Reserved.
#
# This code is modified from Uber Horovod
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import re


# List of regular expressions to ignore environment variables by.
IGNORE_REGEXES = {'BASH_FUNC_.*\(\)', 'OLDPWD'}


def is_exportable(v):
    return not any(re.match(r, v) for r in IGNORE_REGEXES)