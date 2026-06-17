Jewlz FluidForce Simulator - Generated OpenFOAM CFD Case

Geometry:
- Uploaded file converted to meters and written as constant/triSurface/uploadedObject.stl

Run in OpenFOAM:
1. Open an OpenFOAM shell or WSL OpenFOAM environment.
2. cd to this case folder.
3. chmod +x Allrun
4. ./Allrun

Expected workflow:
blockMesh -> surfaceFeatureExtract -> snappyHexMesh -overwrite -> checkMesh -> simpleFoam
foamToVTK -ascii || true

OpenFOAM 1912 compatibility:
- WSL execution copies the case to /tmp/jewlz_fluidforce_openfoam before running to avoid /mnt/c sha1 IOstream errors.
- wallDist method is written inside system/fvSchemes as method meshWave;
- no separate system/wallDist file is generated.
- FOAM_FILEHANDLER=uncollated is used to avoid OpenFOAM sha1 IOstream errors on WSL/Windows workflows.

Reference values:
rho = 51.666771552120764 kg/m^3
mu = 1.871286496118578e-05 Pa*s
nu = 3.6218374787184997e-07 m^2/s
U = (-30 -0 -0) m/s
Aref = 0.0006451599806213381 m^2
lRef = 0.025399999618530275 m
front = +X front

Notes:
- This is a generated baseline CFD case.
- Mesh quality, y+, turbulence model, boundary size, and convergence must be verified.
- For production-grade CFD, refine mesh, add boundary layers, validate Cd/Cl, and compare against experimental or benchmark data.
