import math

import pytest

from scripts.drawing_projected_cylinders import projected_cylinder_circle


def evidence(turn=2*math.pi, radius=.00185):
    samples = [[radius*math.cos(turn*i/32), radius*math.sin(turn*i/32), .002*math.cos(2*turn*i/32)] for i in range(33)]
    return [0,0,0,0,0,1,radius], samples[0]+samples[-1]+[0,1], samples


def project(point, vector=False):
    return [10*x+(0 if vector else t) for x,t in zip(point,[.15,.2,0])]


def test_exact_cylinder_intersection_projects_to_circle_despite_varying_depth():
    result = projected_cylinder_circle(*evidence(), project)
    assert result['diameter_mm'] == pytest.approx(3.7)
    assert result['projected_cylinder_boundary']


@pytest.mark.parametrize('mutation', ['partial','oblique','wrong_radius','double_turn','missing_sample','nan'])
def test_rejects_unsupported_projected_geometry(mutation):
    p,u,s = evidence(4*math.pi if mutation=='double_turn' else math.pi if mutation=='partial' else 2*math.pi)
    if mutation=='oblique': p[3:6]=[1,0,0]
    if mutation=='wrong_radius': p[6]*=2
    if mutation=='missing_sample': s[5]=[]
    if mutation=='nan': p[6]=float('nan')
    assert projected_cylinder_circle(p,u,s,project) is None


def test_nonfinite_projection_cannot_pass_radius_or_winding_checks():
    p,u,s=evidence()
    s[4][0]=float('nan')
    assert projected_cylinder_circle(p,u,s,project) is None
