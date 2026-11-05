# Dataset Request-Format Transformation

## Source and output

- Source: `../raw/dataset.csv`
- Output: `dataset_request_format.csv`
- Output CSV header: `payload,label`
- Transformation preserves each source label and the parsed JSON object's keys and values.

## Synthetic-body convention

Each source JSON object is wrapped as:

```json
{"query_params":{},"body_params":ORIGINAL_OBJECT}
```

The envelope is serialized as compact JSON with UTF-8 characters preserved. This
convention is synthetic: the source dataset contains no request provenance, so
it does not establish that any field actually came from an HTTP request body.
The empty `query_params` map and placement of the full object in
`body_params` are assumptions for compatibility testing only.

## Validation performed

- 75 source records transformed; labels preserved: Normal 25, SQLi 25, XSS 25.
- Every output payload parses as a JSON object with exactly `query_params` and
  `body_params`.
- `query_params` is empty and `body_params` equals the original parsed object.
- No duplicate payloads or conflicting labels were found in the output.
- Importer and normalizer validation should be run separately before training.

This file documents the transformation and its limitations; it does not claim
that the records are real HTTP requests or that the resulting model is accurate.
