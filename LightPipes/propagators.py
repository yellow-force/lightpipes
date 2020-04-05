# -*- coding: utf-8 -*-

"""User can decide to disable dependency here. This will slow down the FFT,
but otherwise numpy.fft is a drop-in replacement so far."""
_USE_PYFFTW = True
_using_pyfftw = False # determined if loading is successful
if _USE_PYFFTW:
    try:
        import pyfftw as _pyfftw
        from pyfftw.interfaces.numpy_fft import fft2 as _fft2
        from pyfftw.interfaces.numpy_fft import ifft2 as _ifft2
        _fftargs = {'planner_effort': 'FFTW_ESTIMATE',
                    'overwrite_input': True,
                    'threads': 4} #negative means use N_cpus according to doc
        _using_pyfftw = True
    except ImportError:
        import warnings
        warnings.warn('LightPipes: Cannot import pyfftw,'
                      + ' falling back to numpy.fft')
if not _using_pyfftw:
    from numpy.fft import fft2 as _fft2
    from numpy.fft import ifft2 as _ifft2
    _fftargs = {}

import numpy as _np
from scipy.special import fresnel as _fresnel

from .field import Field
from . import tictoc

def Fresnel(z, Fin):
    """
    Fout = Fresnel(z, Fin)

    :ref:`Propagates the field using a convolution method. <Fresnel>`

    Args::
    
        z: propagation distance
        Fin: input field
        
    Returns::
     
        Fout: output field (N x N square array of complex numbers).
            
    Example:
    
    :ref:`Two holes interferometer <Young>`

    """
    Fout = Field.shallowcopy(Fin) #no need to copy .field as it will be
    # re-created anyway inside _field_Fresnel()
    Fout.field = _field_Fresnel(z, Fout.field, Fout.dx, Fout.lam)
    return Fout


def _field_Fresnel(z, field, dx, lam):
    """
    Separated the "math" logic out so that only standard and numpy types
    are used.
    
    Parameters
    ----------
    z : float
        Propagation distance.
    field : ndarray
        2d complex numpy array (NxN) of the field.
    dx : float
        In units of sim (usually [m]), spacing of grid points in field.
    lam : float
        Wavelength lambda in sim units (usually [m]).

    Returns
    -------
    ndarray (2d, NxN, complex)
        The propagated field.

    """
    
    """ *************************************************************
    Major differences to Cpp based LP version:
        - dx =siz/N instead of dx=siz/(N-1), more consistent with physics 
            and rest of LP package
        - fftw DLL uses no normalization, numpy uses 1/N on ifft -> omitted
            factor of 1/(2*N)**2 in final calc before return
        - bug in Cpp version: did not touch top row/col, now we extract one
            more row/col to fill entire field. No errors noticed with the new
            method so far
    ************************************************************* """
    tictoc.tic()
    N = field.shape[0] #assert square
    
    kz = 2*_np.pi/lam*z
    cokz = _np.cos(kz)
    sikz = _np.sin(kz)
    
    legacy = True #switch on to numerically compare oldLP/new results
    if legacy:
        siz = N*dx
        dx = siz/(N-1) #like old Cpp code, even though unlogical

    No2 = int(N/2) #"N over 2"
    """The following section contains a lot of uses which boil down to
    2*No2. For even N, this is N. For odd N, this is NOT redundant:
        2*No2 is N-1 for odd N, therefore sampling an even subset of the
        field instead of the whole field. Necessary for symmetry of first
        step involving Fresnel integral calc.
    """
    if _using_pyfftw:
        in_outF = _pyfftw.zeros_aligned((2*N, 2*N),dtype=complex)
        in_outK = _pyfftw.zeros_aligned((2*N, 2*N),dtype=complex)
    else:
        in_outF = _np.zeros((2*N, 2*N),dtype=complex)
        in_outK = _np.zeros((2*N, 2*N),dtype=complex)
    
    """Our grid is zero-centered, i.e. the 0 coordiante (beam axis) is
    not at field[0,0], but field[No2, No2]. The FFT however is implemented
    such that the frequency 0 will be the first element of the output array,
    and it also expects the input to have the 0 in the corner.
    For the correct handling, an fftshift is necessary before *and* after
    the FFT/IFFT:
        X = fftshift(fft(ifftshift(x)))  # correct magnitude and phase
        x = fftshift(ifft(ifftshift(X)))  # correct magnitude and phase
        X = fftshift(fft(x))  # correct magnitude but wrong phase !
        x = fftshift(ifft(X))  # correct magnitude but wrong phase !
    A numerically faster way to achieve the same result is by multiplying
    with an alternating phase factor as done below.
    Speed for N=2000 was ~0.4s for a double fftshift and ~0.1s for a double
    phase multiplication -> use the phase factor approach (iiij).
    """
    # Create the sign-flip pattern for largest use case and 
    # reference smaller grids with a view to the same data for
    # memory saving.
    ii2N = _np.ones((2*N),dtype=float)
    ii2N[1::2] = -1 #alternating pattern +,-,+,-,+,-,...
    iiij2N = _np.outer(ii2N, ii2N)
    iiij2No2 = iiij2N[:2*No2,:2*No2] #slice to size used below
    iiijN = iiij2N[:N, :N]

    RR = _np.sqrt(1/(2*lam*z))*dx*2
    io = _np.arange(0, (2*No2)+1) #add one extra to stride fresnel integrals
    R1 = RR*(io - No2)
    fs, fc = _fresnel(R1)
    fss = _np.outer(fs, fs) #    out[i, j] = a[i] * b[j]
    fsc = _np.outer(fs, fc)
    fcs = _np.outer(fc, fs)
    fcc = _np.outer(fc, fc)
    
    """Old notation (0.26-0.33s):
        temp_re = (a + b + c - d + ...)
        # numpy func add takes 2 operands A, B only
        # -> each operation needs to create a new temporary array, i.e.
        # ((((a+b)+c)+d)+...)
        # since python does not optimize to += here (at least is seems)
    New notation (0.14-0.16s):
        temp_re = (a + b) #operation with 2 operands
        temp_re += c
        temp_re -= d
        ...
    Wrong notation:
        temp_re = a #copy reference to array a
        temp_re += b
        ...
        # changing `a` in-place, re-using `a` will give corrupted
        # result
    """
    temp_re = (fsc[1:, 1:] #s[i+1]c[j+1]
               + fcs[1:, 1:]) #c[+1]s[+1]
    temp_re -= fsc[:-1, 1:] #-scp [p=+1, without letter =+0]
    temp_re -= fcs[:-1, 1:] #-csp
    temp_re -= fsc[1:, :-1] #-spc
    temp_re -= fcs[1:, :-1] #-cps
    temp_re += fsc[:-1, :-1] #sc
    temp_re += fcs[:-1, :-1] #cs
    
    temp_im = (-fcc[1:, 1:] #-cpcp
               + fss[1:, 1:]) # +spsp
    temp_im += fcc[:-1, 1:] # +ccp
    temp_im -= fss[:-1, 1:] # -ssp
    temp_im += fcc[1:, :-1] # +cpc
    temp_im -= fss[1:, :-1] # -sps
    temp_im -= fcc[:-1, :-1] # -cc
    temp_im += fss[:-1, :-1]# +ss
    
    temp_K = 1j * temp_im # a * b creates copy and casts to complex
    temp_K += temp_re
    temp_K *= iiij2No2
    temp_K *= 0.5
    in_outK[(N-No2):(N+No2), (N-No2):(N+No2)] = temp_K
    
    in_outF[(N-No2):(N+No2), (N-No2):(N+No2)] \
        = field[(N-2*No2):N,(N-2*No2):N] #cutting off field if N odd (!)
    in_outF[(N-No2):(N+No2), (N-No2):(N+No2)] *= iiij2No2
    
    tictoc.tic()
    in_outK = _fft2(in_outK, **_fftargs)
    in_outF = _fft2(in_outF, **_fftargs)
    t_fft1 = tictoc.toc()
    
    in_outF *= in_outK
    
    in_outF *= iiij2N
    tictoc.tic()
    in_outF = _ifft2(in_outF, **_fftargs)
    t_fft2 = tictoc.toc()
    #TODO check normalization if USE_PYFFTW
    
    Ftemp = (in_outF[No2:N+No2, No2:N+No2]
             - in_outF[No2-1:N+No2-1, No2:N+No2])
    Ftemp += in_outF[No2-1:N+No2-1, No2-1:N+No2-1]
    Ftemp -= in_outF[No2:N+No2, No2-1:N+No2-1]
    comp = complex(cokz, sikz)
    Ftemp *= 0.25 * comp
    Ftemp *= iiijN
    field = Ftemp #reassign without data copy
    ttotal = tictoc.toc()
    t_fft = t_fft1 + t_fft2
    t_outside = ttotal - t_fft
    debug_time = False
    if debug_time:
        print('Time total = fft + rest: {:.2f}={:.2f}+{:.2f}'.format(
            ttotal, t_fft, t_outside))
    return field


def Forward(z, sizenew, Nnew, Fin):
    """
    Fout = Forward(z, sizenew, Nnew, Fin)

    :ref:`Propagates the field using direct integration. <Forward>`

    Args::
    
        z: propagation distance
        Fin: input field
        
    Returns::
     
        Fout: output field (N x N square array of complex numbers).
            
    Example:
    
    :ref:`Diffraction from a circular aperture <Diffraction>`
    
    """
    """CPP
        CMPLXVEC FieldNew;
        FieldNew.resize(new_n, vector<complex<double> >(new_n,1.0));
        int i_old, i_new, j_old, j_new;
        int old_n,  on21, nn21;
        double old_size;
        double  x_new, y_new, dx_new, dx_old;
        double  P1, P2, P3, P4, R22, dum; 
        double fc1, fs1, fc2, fs2, fc3, fs3, fc4, fs4, fr, fi;
        double c4c1, c2c3, c4s1, s4c1, s2c3, c2s1, s4c3, s2c1, c4s3, s2s3, \
                s2s1, c2s3, s4s1, c4c3, s4s3, c2c1;
    
    """
    Fout = Field.begin(sizenew, Fin.lam, Nnew)
    
    field_in = Fin.field
    field_out = Fout.field
    
    field_out[:,:] = 0.0 #default is ones, clear
    """
        old_size = size;
        old_n    = N;
    
        on21     = (int)old_n/2 + 1;
        nn21     = (int)new_n/2 + 1;
        dx_new   = new_size/(new_n-1);
        dx_old   = old_size/(old_n-1);
    
        R22=sqrt(1./(2.*lambda*z));          
        fs1=fc1=fs2=fc2=fs3=fc3=fs4=fc4=0.; /* to make the compiler happy */    
    """
    old_size = Fin.siz
    old_n    = Fin.N
    new_size = sizenew #renaming to match cpp code
    new_n = Nnew

    # on21     = int(old_n/2) + 1
    # nn21     = int(new_n/2) + 1
    on2     = int(old_n/2)
    nn2     = int(new_n/2) #read "new n over 2"
    dx_new   = new_size/(new_n-1)
    dx_old   = old_size/(old_n-1)
    #TODO again, dx seems better defined without -1, check this
    
    R22 = _np.sqrt(1/(2*Fin.lam*z))
    """
    for (i_new = 0; i_new < new_n; i_new++){
        x_new = (i_new - nn21 + 1) * dx_new; 
        for (j_new = 0; j_new < new_n; j_new++){
            y_new = (j_new - nn21 + 1) * dx_new;
    """
    # Y_new, X_new = Fout.mgrid_cartesian #!NOT equivalent since dx ~ N-1
    X_new = _np.arange(-nn2, new_n-nn2) * dx_new
    Y_new = X_new
    for i_new in range(new_n):
        x_new = X_new[i_new] #slice lookup takes MUCH longer than re-calc!
        for j_new in range(new_n):
            y_new = X_new[j_new]
            """
            FieldNew.at(i_new).at(j_new) = complex<double>(0.,0.);
            for (i_old = 0; i_old < old_n; i_old++){
                int io=i_old-on21+1; /* bug repaired: +1 added to formula */
                for (j_old = 0; j_old < old_n; j_old++){
                    int jo=j_old-on21+1; /* bug repaired: +1 added to formula */   
            """
            X_old = _np.arange(-on2, old_n-on2) * dx_old
            PP1 = R22*(2*(X_old-x_new)+dx_old)
            PP3 = R22*(2*(X_old-x_new)-dx_old)
            Fs1, Fc1 = _fresnel(PP1)
            Fs3, Fc3 = _fresnel(PP3)
            
            for i_old in range(old_n):
                # io = i_old - on2
                # x_old = io * dx_old
                # assert x_old == X_old[i_old]
                # P1 = R22*(2*(x_old-x_new)+dx_old)
                # P3 = R22*(2*(x_old-x_new)-dx_old)
                # fs1, fc1 = _fresnel(P1)
                # fs3, fc3 = _fresnel(P3)
                fs1, fc1 = Fs1[i_old], Fc1[i_old]
                fs3, fc3 = Fs3[i_old], Fc3[i_old]
                
                Y_old = _np.arange(-on2, old_n-on2) * dx_old
                
                PP2 = R22*(2*(Y_old-y_new)-dx_old)
                PP4 = R22*(2*(Y_old-y_new)+dx_old)
                Fs2, Fc2 = _fresnel(PP2)
                Fs4, Fc4 = _fresnel(PP4) #now arrays with index [j_old]
                
                
                C4c1=Fc4*fc1
                C2s3=Fc2*fs3
                C4s1=Fc4*fs1
                S4c1=Fs4*fc1
                S2c3=Fs2*fc3
                C2s1=Fc2*fs1
                S4c3=Fs4*fc3
                S2c1=Fs2*fc1
                C4s3=Fc4*fs3
                S2s3=Fs2*fs3
                S2s1=Fs2*fs1
                C2c3=Fc2*fc3
                S4s1=Fs4*fs1
                C4c3=Fc4*fc3
                C4c1=Fc4*fc1
                S4s3=Fs4*fs3
                C2c1=Fc2*fc1
                
                
                for j_old in range(old_n):
                    # jo = j_old - on2
                    # y_old = jo * dx_old
                    # assert y_old == Y_old[j_old]
                    
                    """
                    P1=R22*(2*(dx_old*io-x_new)+dx_old);
                    P2=R22*(2*(dx_old*jo-y_new)-dx_old);
                    P3=R22*(2*(dx_old*io-x_new)-dx_old);
                    P4=R22*(2*(dx_old*jo-y_new)+dx_old);
                    dum=fresnl(P1,&fs1, &fc1);
                    dum=fresnl(P2,&fs2, &fc2);
                    dum=fresnl(P3,&fs3, &fc3);
                    dum=fresnl(P4,&fs4, &fc4);
                    """
                    # P2 = R22*(2*(y_old-y_new)-dx_old)
                    # P4 = R22*(2*(y_old-y_new)+dx_old)
                    # fs2, fc2 = _fresnel(P2)
                    # assert fs2 == Fs2[j_old]
                    # fs4, fc4 = _fresnel(P4)
                    
                    # fs2, fc2 = Fs2[j_old], Fc2[j_old]
                    # fs4, fc4 = Fs4[j_old], Fc4[j_old]
                    
                    """
                    c4c1=fc4*fc1;
                    c2s3=fc2*fs3;
                    c4s1=fc4*fs1;
                    s4c1=fs4*fc1;
                    s2c3=fs2*fc3;
                    c2s1=fc2*fs1;
                    s4c3=fs4*fc3;
                    s2c1=fs2*fc1;
                    c4s3=fc4*fs3;
                    s2s3=fs2*fs3;
                    s2s1=fs2*fs1;
                    c2c3=fc2*fc3;
                    s4s1=fs4*fs1;
                    c4c3=fc4*fc3;
                    c4c1=fc4*fc1;
                    s4s3=fs4*fs3;
                    c2c1=fc2*fc1;
                    """
                    
                    c4c1=C4c1[j_old]
                    c2s3=C2s3[j_old]
                    c4s1=C4s1[j_old]
                    s4c1=S4c1[j_old]
                    s2c3=S2c3[j_old]
                    c2s1=C2s1[j_old]
                    s4c3=S4c3[j_old]
                    s2c1=S2c1[j_old]
                    c4s3=C4s3[j_old]
                    s2s3=S2s3[j_old]
                    s2s1=S2s1[j_old]
                    c2c3=C2c3[j_old]
                    s4s1=S4s1[j_old]
                    c4c3=C4c3[j_old]
                    c4c1=C4c1[j_old]
                    s4s3=S4s3[j_old]
                    c2c1=C2c1[j_old]
                    
                    """       
                    fr=0.5*real(Field.at(i_old).at(j_old));
                    fi=0.5*imag(Field.at(i_old).at(j_old));             
                    FieldNew.at(i_new).at(j_new) += complex<double>(
                        fr*( c2s3+c4s1+s4c1+s2c3-c2s1-s4c3-s2c1-c4s3) +
                        fi*(-s2s3+s2s1+c2c3-s4s1-c4c3+c4c1+s4s3-c2c1), 
                        fr*(-c4c1+s2s3+c4c3-s4s3+c2c1-s2s1+s4s1-c2c3) +
                        fi*( c2s3+s2c3+c4s1+s4c1-c4s3-s4c3-c2s1-s2c1)
                        );                                                        
                    """
                    fr = 0.5 * field_in[j_old, i_old].real #note swapped i,j!
                    fi = 0.5 * field_in[j_old, i_old].imag
                    
                    field_out[j_new, i_new] += complex(
                        fr*( c2s3+c4s1+s4c1+s2c3-c2s1-s4c3-s2c1-c4s3)
                        + fi*(-s2s3+s2s1+c2c3-s4s1-c4c3+c4c1+s4s3-c2c1),
                        fr*(-c4c1+s2s3+c4c3-s4s3+c2c1-s2s1+s4s1-c2c3)
                        + fi*( c2s3+s2c3+c4s1+s4c1-c4s3-s4c3-c2s1-s2c1)
                        )
    return Fout

def TODOForvard(self, z, Fin):
    """
    Fout = Forvard(z, Fin)

    :ref:`Propagates the field using a FFT algorithm. <Forvard>`

    Args::
    
        z: propagation distance
        Fin: input field
        
    Returns::
     
        Fout: output field (N x N square array of complex numbers).
            
    Example:
    
    :ref:`Diffraction from a circular aperture <Diffraction>`
    
    """
    # return self.thisptr.Forvard(z, Fin)
    """CPP
        fftw_complex* in_out = (fftw_complex*) fftw_malloc(sizeof(fftw_complex) * N * N);
        if (in_out == NULL) return Field;
        int ii, ij, n12;
        long ik, ir;
        double z,z1,cc;
        double sw, sw1, bus, abus, pi2, cab, sab, kz, cokz, sikz;
    
        pi2=2.*3.141592654;
        z=fabs(zz);
        kz = pi2/lambda*z;
        cokz = cos(kz);
        sikz = sin(kz);
        ik=0;
        ii=ij=1;
        for (int i=0;i<N; i++){
            for (int j=0;j<N; j++){
                in_out[ik][0] = Field.at(i).at(j).real()*ii*ij;
                in_out[ik][1] = Field.at(i).at(j).imag()*ii*ij; 
                ik++;
                ij=-ij;
            }
            ii=-ii;
        }
        fftw_plan planF = fftw_plan_dft_2d (N, N, in_out, in_out, FFTW_FORWARD, FFTW_ESTIMATE);
        if (planF == NULL) return Field;
        fftw_plan planB = fftw_plan_dft_2d (N, N, in_out, in_out, FFTW_BACKWARD, FFTW_ESTIMATE);
        if (planB == NULL) return Field;  
        // Spatial filter, (c)  Gleb Vdovin  1986:  
        if (zz>=0.) fftw_execute(planF);
        else fftw_execute(planB);
        if(zz >= 0.){
           z1=z*lambda/2.;
           n12=int(N/2);
           ik=0;
           for (int i=0;i<N; i++){
               for (int j=0;j<N; j++){ 
                   sw=((i-n12)/size);
                   sw *= sw;
                   sw1=((j-n12)/size);
                   sw1 *= sw1;
                   sw += sw1; 
                   bus=z1*sw;
                   ir = (long) bus;
                   abus=pi2*(ir- bus);
                   cab=cos(abus);
                   sab=sin(abus);
                   cc=in_out[ik][0]*cab-in_out[ik][1]*sab;
                   in_out[ik][1]=in_out[ik][0]*sab+in_out[ik][1]*cab;
                   in_out[ik][0]=cc;
                   ik++;
               }
           }
        }
        else { 
          z1=z*lambda/2.;
          n12=int(N/2);
          ik=0;
          for (int i=0;i<N; i++){
            for (int j=0;j<N; j++){ 
                sw=((i-n12)/size);
                sw *= sw;
                sw1=((j-n12)/size);
                sw1 *= sw1;
                sw += sw1; 
                bus=z1*sw;
                ir = (long) bus;
                abus=pi2*(ir- bus);
                cab=cos(abus);
                sab=sin(abus);
                cc=in_out[ik][0]*cab + in_out[ik][1]*sab;
                in_out[ik][1]= in_out[ik][1]*cab-in_out[ik][0]*sab;
                in_out[ik][0]=cc;
                ik++;
            }
          }
        }
        if (zz>=0.) fftw_execute(planB);
        else fftw_execute(planF);
        ik=0;
        ii=ij=1;
        for (int i=0;i<N; i++){    
            for (int j=0;j<N; j++ ){
                Field.at(i).at(j) = complex<double>((in_out[ik][0]*ii*ij * cokz - in_out[ik][1]*ii*ij * sikz)/N/N,\
                                                   ( in_out[ik][1]*ii*ij * cokz + in_out[ik][0]*ii*ij * sikz)/N/N );
                ij=-ij;
                ik++;
            }
            ii=-ii;
        }
        fftw_destroy_plan(planF);
        fftw_destroy_plan(planB);
        fftw_free(in_out);
        fftw_cleanup();
        return Field;
    """
    raise NotImplementedError()


def TODOSteps(self, z, nstep, refr, Fin):
    """
    Fout = Steps(z, nstep, refr, Fin)
                 
    :ref:`Propagates the field a distance, nstep x z, in nstep steps in a
    medium with a complex refractive index stored in the
    square array refr. <Steps>`

    Args::
    
        z: propagation distance per step
        nstep: number of steps
        refr: refractive index (N x N array of complex numbers)
        Fin: input field
        
    Returns::
      
        Fout: ouput field (N x N square array of complex numbers).
        
    Example:
    
    :ref:`Propagation through a lens like medium <lenslikemedium>`
    
    """
    # return self.thisptr.Steps(z, nstep, refr, Fin) 
    """CPP
        double  delta, delta2, Pi4lz, AA, band_pow, K, dist, fi,i_left, i_right;
        std::complex<double> uij, uij1, uij_1, ui1j, ui_1j, medium;
        int i, j, jj, ii;
        int  istep;
        vectors v; //the structure vectors is used to pass a lot of variables to function elim
        if (doub1 !=0.){
            printf("error in 'Steps(z,nsteps, refr, Fin)': Spherical coordinates. Use Fout=Convert(Fin) first.\n");
            return Field;
        }
        v.a.resize(N+3);
        v.b.resize(N+3);
        v.c.resize(N+3);
        v.u.resize(N+3);
        v.u1.resize(N+3);
        v.u2.resize(N+3);
        v.alpha.resize(N+3);
        v.beta.resize(N+3);
        v.p.resize(N+3);
    
        K=2.*Pi/lambda;
        z=z/2.;
        Pi4lz = 4.*Pi/lambda/z;
        std::complex<double> imPi4lz (0.0,Pi4lz);
        delta=size/((double)(N-1.));
        delta2 = delta*delta;
    
    /* absorption at the borders is described here */
        AA= -10./z/nstep; /* total absorption */
        band_pow=2.;   /* profile of the absorption border, 2=quadratic*/
    /* width of the absorption border */
        i_left=N/2+1.0-0.4*N;
        i_right=N/2+1.0+0.4*N;
    /* end absorption */
    
        for ( i=1; i <= N; i++){
            v.u2.at(i) = 0.0;
            v.a.at(i) = std::complex<double>( -1./delta2 , 0.0 );
            v.b.at(i) = std::complex<double>( -1./delta2 , 0.0 );
        }
        medium= 0.0;
        dist =0.;
    
    /*  Main  loop, steps here */
        for(istep = 1; istep <= nstep ; istep ++){
            dist=dist + 2.*z;
    
    /*  Elimination in the direction i, halfstep  */
            for (i=0; i< N; i++){
                for( j=0; j< N; j++){
                    double  fi;
                    fi=0.25*K*z*(refr.at(i).at(j).real()-1.0);
                    Field.at(i).at(j) *= exp(_j * fi);
                }
            }
    
            for(jj=2; jj <= N-2; jj += 2){
                j=jj;
                for (i=2; i <= N-1; i++){
    
                    uij=Field.at(i-1).at(j-1);
                    uij1=Field.at(i-1).at(j);
                    uij_1=Field.at(i-1).at(j-2);
                    v.p.at(i) = -1.0/delta2 * (uij_1 + uij1 -2.0 * uij) + imPi4lz * uij;           
                }
                for ( i=1; i <= N; i++){
                    
                    if (refr.at(i-1).at(j-1).imag() == 0.0) medium = std::complex<double> (medium.real() , 0.0);				
                    else medium = std::complex<double>(medium.real() , -2.0 * Pi * refr.at(i-1).at(j-1).imag() / lambda);                              
    
                    v.c.at(i) = std::complex<double>( -2.0 / delta2, Pi4lz + medium.imag() );
     
    ///* absorption borders are formed here */
                    if(  i <= i_left){
                        double iii=i_left-i+1;
                        v.c.at(i) = std::complex<double> (v.c.at(i).real() , v.c.at(i).imag() - (AA*K)*pow((double) iii/ ((double)(i_left)),band_pow));
                    }
    
                    if(  i >= i_right){ 
                        double iii=i-i_right+1;
                        double im=N-i_right+1;
                        v.c.at(i) = std::complex<double> (v.c.at(i).real() , v.c.at(i).imag() - (AA*K)*pow((double) iii/ ((double)(im)),band_pow));
                    }
    ///* end absorption */
                }
    
                elim(v,N);
                for ( i=1; i<= N; i++){
                    Field.at(i-1).at(j-2) = v.u2.at(i);
                    v.u2.at(i)=v.u.at(i);
                }
                j=jj+1;
                for ( i=2; i <= N-1; i++){
                    uij=Field.at(i-1).at(j-1);
                    uij1=Field.at(i-1).at(j);
                    uij_1=Field.at(i-1).at(j-2);
                    v.p.at(i) = -1.0/delta2 * (uij_1 + uij1 -2.0 * uij) + imPi4lz * uij;
                }
                for ( i=1; i <= N; i++){
                    if (refr.at(i-1).at(j-1).imag() == 0.0) medium = std::complex<double>( medium.real() , 0.0 );
                    else medium = std::complex<double>(medium.real() ,  -2.*Pi*refr.at(i-1).at(j-1).imag()/lambda);
                    v.c.at(i) = std::complex<double>(-2.0/delta2, Pi4lz + medium.imag());
    
    ///* absorption borders are formed here */
                    if( i <= i_left){
                        double iii=i_left-i+1;
                        v.c.at(i) = std::complex<double>( v.c.at(i).real() , v.c.at(i).imag() - (AA*K)*pow((double) iii/ ((double)(i_left)),band_pow) );
                    }
    
                    if( i >= i_right){ 
                        double iii=i-i_right;
                        double im=N-i_right+1;
                        //c.at(i).imag(c.at(i).imag() - (AA*2.0*K)*pow((double) iii/ ((double)(im)),band_pow)); /* Gleb's original */
                        v.c.at(i) = std::complex<double>(  v.c.at(i).real() , v.c.at(i).imag() - (AA*K)*pow((double) iii/ ((double)(im)),band_pow) );
                    }
    ///* end absorption */
                }
                elim(v,N);
                for ( i=1; i <= N; i++){
                    Field.at(i-1).at(j-2) = v.u2.at(i);
                    v.u2.at(i)=v.u.at(i);
                }
            }
            for ( i=1; i <= N; i++){
                Field.at(i-1).at(N-1) = v.u2.at(i);
            }
            for ( i=0; i < N; i++){
                for( j=0; j < N; j++){
                    fi=0.5*K*z*(refr.at(i).at(j).real()-1.0);
                    Field.at(i).at(j) *= exp(_j * fi);
                }
            }
    
    /* Elimination in the j direction is here, halfstep */
    
            for ( i=1; i <= N; i++){
                v.u2.at(i)=0.0;
            }
            for(ii=2; ii <= N-2; ii += 2){
                i=ii;
                for ( j=2; j <= N-1; j++){
                    uij=Field.at(i-1).at(j-1);
                    ui1j=Field.at(i).at(j-1);
                    ui_1j=Field.at(i-2).at(j-1);
                    v.p.at(j) = -1.0/delta2 * (ui_1j + ui1j -2.0 * uij) + imPi4lz * uij;
                }
                for ( j=1; j <= N; j++){
                    if (refr.at(i-1).at(j-1).imag() == 0.0) medium =std::complex<double>(medium.real() , 0.0);
                    else medium= std::complex<double>( medium.real() , -2.0 * Pi * refr.at(i-1).at(j-1).imag() / lambda );
                    v.c.at(j) = std::complex<double>( -2.0 / delta2 , Pi4lz + medium.imag() );	
    
    
    /* absorption borders are formed here */
                    if( j <= i_left){
                        size_t iii=(long)i_left-j;
                        v.c.at(j) = std::complex<double>( v.c.at(j).real() , v.c.at(j).imag() - (AA*K)*pow((double) iii/ ((double)(i_left)),band_pow) );
                    }
    
                    if( j >= i_right){ 
                        size_t iii=j-(long)i_right;
                        double im=N-i_right+1;
                        v.c.at(j) = std::complex<double>( v.c.at(j).real() , v.c.at(j).imag() - (AA*K)*pow((double) iii/ ((double)(im)),band_pow) );
                    }
    //* end absorption */
                }
                elim(v,N);
    
                for ( j=1; j<= N; j++){
                    Field.at(i-2).at(j-1) = v.u2.at(j);
                    v.u2.at(j)=v.u.at(j);
                }
                i=ii+1;
                for ( j=2; j <= N-1; j++){
                    uij=Field.at(i-1).at(j-1);
                    ui1j=Field.at(i).at(j-1);
                    ui_1j=Field.at(i-2).at(j-1);
                    v.p.at(j) = -1.0/delta2 * (ui_1j + ui1j -2.0 * uij) + imPi4lz * uij;
                }
                for ( j=1; j <= N; j++){
                    if (refr.at(i-1).at(j-1).imag() == 0.0) medium = std::complex<double>( medium.real() , 0.0);
                        else medium = std::complex<double>( medium.real() , -2.*Pi*refr.at(i-1).at(j-1).imag()/lambda );
                        v.c.at(j) = std::complex<double>( -2.0/delta2 , Pi4lz + medium.imag() );
    /* absorption borders are formed here */
                    if( j <= i_left){
                        size_t  iii=(long )i_left-j;
                        v.c.at(j) = std::complex<double>( v.c.at(j).real(), v.c.at(j).imag() - (AA*K)*pow((double) iii/ ((double)(i_left)),band_pow) );
                    }
    
                    if( j >= i_right){ 
                        size_t  iii=j-(long )i_right;
                        double im=N-i_right+1;
                        v.c.at(j) = std::complex<double>( v.c.at(j).real() , v.c.at(j).imag() - (AA*K)*pow((double) iii/ ((double)(im)),band_pow) );
                    }
    /* end absorption */
                }
    
                elim(v,N);
                for ( j=1; j <= N; j++){
                    Field.at(i-2).at(j-1) = v.u2.at(j);
                    v.u2.at(j)=v.u.at(j);
                }
            }
    
            for ( j=2; j <= N; j++){
                Field.at(i-1).at(j-2) = v.u2.at(j);
            }
    
    ///* end j */ 
    
            }
            for ( i=0; i < N; i++){
                for(j=0; j < N; j++){
                    fi=0.25*K*z*(refr.at(i).at(j).real()-1.0);
                    Field.at(i).at(j) *=  exp(_j * fi);
                }
            }
        return Field;
    """
    raise NotImplementedError()
    



