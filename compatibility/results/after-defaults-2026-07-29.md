# Compatibility corpus report

- Programs: 50
- Compile passed: 37
- Uninterrupted output matched CPython: 36
- Same-process resume passed: 35
- New-process resume matched CPython: 35
- Compatibility rate: 70.0%
- Cross-platform corpus results: not run
- Timing fields are raw wall-clock diagnostics; CPython includes process startup, so this report does not calculate a slowdown ratio

## Compile failures

| Diagnostic | Count |
| --- | ---: |
| `only positional parameters are supported` | 3 |
| `Assign (chained assignment)` | 1 |
| `AugAssign (augmented assignment to non-name)` | 1 |
| `Call (starred call arguments)` | 1 |
| `ClassDef` | 1 |
| `Compare (chained comparison)` | 1 |
| `DictComp (expression)` | 1 |
| `FormattedValue (f-string conversion)` | 1 |
| `ListComp (expression)` | 1 |
| `SetComp (expression)` | 1 |
| `Try (only try/finally is supported)` | 1 |

## Programs

| Program | Compile | Run | Same process | New process | Failure |
| --- | --- | --- | --- | --- | --- |
| `assignment_chained` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `backtracking_subsets` | passed | passed | passed | passed |  |
| `bytes_hex_digest` | passed | passed | passed | passed |  |
| `class_accumulator` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `comparison_chained` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `comprehension_dict` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `comprehension_list` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `comprehension_set` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `control_nested_loops` | passed | passed | passed | passed |  |
| `control_try_finally` | passed | passed | passed | passed |  |
| `data_grouping` | passed | passed | passed | passed |  |
| `data_histogram` | passed | passed | passed | passed |  |
| `data_rolling_average` | passed | passed | passed | passed |  |
| `default_clamp_bounds` | passed | passed | passed | passed |  |
| `default_memo_fibonacci` | passed | passed | passed | passed |  |
| `default_parser_separator` | passed | passed | passed | passed |  |
| `dynamic_coin_change` | passed | passed | passed | passed |  |
| `dynamic_lcs` | passed | passed | passed | passed |  |
| `exception_handler` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `fstring_conversion` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `graph_breadth_first` | passed | passed | passed | passed |  |
| `graph_depth_first` | passed | passed | passed | passed |  |
| `graph_shared_cycle` | passed | passed | passed | passed |  |
| `hash_rolling` | passed | passed | passed | passed |  |
| `hash_sha256_chunks` | passed | passed | failed | not_run | unsupported_language_or_runtime |
| `iteration_dictionary` | passed | failed | not_run | not_run | continuum_error |
| `json_aggregate` | passed | passed | passed | passed |  |
| `json_transform` | passed | passed | passed | passed |  |
| `keyword_only_format` | failed | not_run | not_run | not_run | continuum_error |
| `kwargs_merge` | failed | not_run | not_run | not_run | continuum_error |
| `math_matrix_multiply` | passed | passed | passed | passed |  |
| `math_primes` | passed | passed | passed | passed |  |
| `math_statistics` | passed | passed | passed | passed |  |
| `random_walk` | passed | passed | passed | passed |  |
| `recursion_factorial` | passed | passed | passed | passed |  |
| `recursion_fibonacci` | passed | passed | passed | passed |  |
| `recursion_mutual` | passed | passed | passed | passed |  |
| `search_binary` | passed | passed | passed | passed |  |
| `search_linear` | passed | passed | passed | passed |  |
| `simulation_inventory` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `sort_bubble` | passed | passed | passed | passed |  |
| `sort_insertion` | passed | passed | passed | passed |  |
| `sort_selection` | passed | passed | passed | passed |  |
| `starred_call` | failed | not_run | not_run | not_run | unsupported_language_or_runtime |
| `text_bracket_balance` | passed | passed | passed | passed |  |
| `text_normalize` | passed | passed | passed | passed |  |
| `text_palindrome_scan` | passed | passed | passed | passed |  |
| `text_run_length` | passed | passed | passed | passed |  |
| `text_word_frequency` | passed | passed | passed | passed |  |
| `varargs_total` | failed | not_run | not_run | not_run | continuum_error |
