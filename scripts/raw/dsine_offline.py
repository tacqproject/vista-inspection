"""Avoid the redundant ImageNet download before loading the full DSINE state.

The installed hubconf subsequently calls load_state_dict(..., strict=True),
which restores ALL encoder and decoder weights from /dsine/checkpoints/dsine.pt.
The shared image is unchanged; this wrapper is mounted only in RAW jobs.
"""
import runpy
import geffnet

_create_model = geffnet.create_model


def from_full_checkpoint(*args, **kwargs):
    kwargs['pretrained'] = False
    return _create_model(*args, **kwargs)


geffnet.create_model = from_full_checkpoint
runpy.run_path('/dsine/infer_normals.py', run_name='__main__')
