# Frozen embedder parity reference

`embedder_parity_reference.npy` contains 20 x 384 normalized float32 vectors
for `embedder_parity_corpus.txt`, in corpus order. The source model is
`sentence-transformers/all-MiniLM-L6-v2`.

The reference was frozen after the public fixture produced a maximum
element-wise difference of `2.38e-07` between sentence-transformers and the
vendored ONNX path. SHA-256 of the checked-in NPY file:
`8ac91cdde461e6b8d456e715facbccc8bb0753174922b53ba44235000082e014`.

The required CI lane reads this file offline. The explicit
`CLAUDE_KB_PARITY_DB` lane still compares an operator-provided corpus against a
live sentence-transformers installation.
