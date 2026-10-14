# pytorch3d is only used for quaternion_to_matrix — replace it (pytorch3d is a
# long source build) with the standard formula
p = "scene/gaussian_model.py"
s = open(p).read()
old = "from pytorch3d.transforms import quaternion_to_matrix"
assert old in s, "PGSR changed — check the pytorch3d import"
s = s.replace(old, '''
def quaternion_to_matrix(q):
    q = torch.nn.functional.normalize(q, dim=-1)
    r, i, j, k = q.unbind(-1)
    two_s = 2.0 / (q * q).sum(-1)
    o = torch.stack((1 - two_s * (j * j + k * k), two_s * (i * j - k * r), two_s * (i * k + j * r),
                     two_s * (i * j + k * r), 1 - two_s * (i * i + k * k), two_s * (j * k - i * r),
                     two_s * (i * k - j * r), two_s * (j * k + i * r), 1 - two_s * (i * i + j * j)), -1)
    return o.reshape(q.shape[:-1] + (3, 3))
''')
open(p, "w").write(s)
