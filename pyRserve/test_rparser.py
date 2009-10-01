import struct
from numpy import array, ndarray
from numpy.core.records import recarray, record
###
import rtypes, rparser

def shaped_array(data, dtype, shape):
    arr = array(data, dtype=dtype)
    arr.shape = shape
    return arr


r2pyExpressions = [
    ('1',                                       1.0),
    ('c(1, 2)',                                 array([1.0, 2.0])),
    ('seq(1, 5)',                               array(range(1, 6), dtype=int)),
    ('list("otto", "gustav")',                  ["otto", "gustav"]),
    ('list(husband="otto", wife="erna")',       rparser.TaggedList(["husband", "wife"], ["otto", "erna"])),
    ('list(n="Fred", no.c=2, c.ages=c(4,7))',   rparser.TaggedList(["n", "no.c", "c.ages"],["Fred",2.,array([4.,7.])])),
    ('array(1:20, dim=c(4, 5))',                shaped_array(range(1,21), int, (4, 5))),
    #
    #('x<-1:20; y<-x*2; lm(y~x)',                ????),
    # Environment
    #('parent.env',                              [1,2]),
    ]


###############################################3

def test_rExprGenerator():
    '''
    @Brief Main test function generator called from py.test. It generates different test arguments which
           are then fed into the actual testing function "rExprTester()" below.
    '''
    for rExpr, pyExpr in r2pyExpressions:
        if not rExpr in binaryRExpressions.binaryRExpressions:
            # seems like the r2pyExpressions above has changed, but the binaryRExpressions was not rebuilt.
            # Do this now and reload the module:
            createBinaryRExpressions()
            reload(binaryRExpressions)
        yield rExprTester, rExpr, pyExpr, binaryRExpressions.binaryRExpressions[rExpr]


##############################################################

def compareArrays(arr1, arr2):
    def _compareArrays(arr1, arr2):
        assert arr1.shape == arr2.shape
        for idx in range(len(arr1)):
            if isinstance(arr1[idx], ndarray):
                _compareArrays(arr1[idx], arr2[idx])
            else:
                assert arr1[idx] == arr2[idx]
    try:
        _compareArrays(arr1, arr2)
    except TypeError:  #AssertionError:
        return False
    return True


def rExprTester(rExpr, pyExpr, rBinExpr):
    '''
    @Brief  Actual test function called via py.test and the generator "test_rExprGenerator() above.
    @Param  rExpr    <string>             The r expression from r2pyExpressions above
    @Param  pyExpr   <python expression>  The python expression from r2pyExpressions above
    @Param  rBinExpr <string>             rExpr translated by r into its binary (network) representation
    '''
    qTypeCode = struct.unpack('b', rBinExpr[8])[0]
    #
    v = rparser.rparse(rBinExpr)
    if isinstance(v, ndarray):
        compareArrays(v, pyExpr)
    elif v.__class__.__name__ == 'TaggedList':
        # do comparision of string representation for now ...
        assert repr(v) == repr(pyExpr)
    else:
        assert v == pyExpr
        
#    # In python some data types are missing, like 'short' or 'float', etc. In order to serialize
#    # them correctly, a hint has to be given to the serializer. The hint is calculated by looking
#    # into the binary q expression calculated from the qExpr. In position 8 it contains the typecode.
#    # Only typecodes for atomic items are passed in (not for dicts, errors, tables, etc)
#    qTypeHint = qTypeCode if qTypeCode < 0x60 else None
#    if type(pyExpr) == dict:
#        # Python dictionaries do not keep their keys and values in a specific order (unlike q dictionaries),
#        # so a normal test would fail here. That's way we run a slightly modified test for this case:
#        assert qparse(qserialize(pyExpr, messageType=qtypes.QRESPONSE, qTypeHint=qTypeHint)) == pyExpr
#    else:
#        assert qserialize(pyExpr, messageType=qtypes.QRESPONSE, qTypeHint=qTypeHint) == qBinExpr


def hexString(aString):
    'convert a binary string in its hexadecimal representation, like "\x00\x01..."'
    return ''.join([r'\x%02x' % ord(c) for c in aString])

def createBinaryRExpressions():
    '''
    Translates r-expressions from r2pyExpressions into their binary network representations.
    The results will be stored in a python module called "binaryRExpressions.py" which
    is then imported by this module for checking whether the rparser and the rserializer
    produce correct results.
    Running this module requires that R is accessible through PATH.
    '''
    import subprocess, socket, time
    RPORT = 6311
    # Start Rserve
    rProc = subprocess.Popen(['R', 'CMD', 'Rserve.dbg'], stdout=open('/dev/null'))
    # wait a moment until Rserve starts listening on RPORT
    time.sleep(1.0)
    #import pdb;pdb.set_trace()
    
    try:
        # open a socket connection to Rserve
        r = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        r.connect(('', RPORT))
        
        hdr = r.recv(1024)
        assert hdr.startswith('Rsrv01') # make sure we are really connected with rserv
        
        # Create the result file, and write some preliminaries as well as correct code for a dictionary
        # holding the results from calls to Rserve:
        fp = open('binaryRExpressions.py', 'w')
        fp.write("# This file is autogenerated from %s\n" % __file__)
        fp.write("# It contains the translation of r expressions into their \n"
                 "# (network-) serialized representation.\n\n")
        fp.write("binaryRExpressions = {\n")
        for rExpr, pyExpr in r2pyExpressions:
            # make a call to Rserve, sending a valid r expression and then reading the
            # result from the socket:
            # First send header, containing length of data packet part:
            l = len(rExpr)
            # The data packet contains trailing padding zeros to be always a multiple of 4 in length:
            multi4Len = l + (4-divmod(l, 4)[1])
            r.send('\x03\x00\x00\x00' + struct.pack('<i', 4 + multi4Len) + 8*'\x00')
            # send data:
            stringHeader = struct.pack('B', rtypes.DT_STRING) + struct.pack('<i', multi4Len)[:3]
            r.send(stringHeader + rExpr + (multi4Len-l)*'\x00')
            time.sleep(0.1)
            binRExpr = r.recv(1024)
            fp.write("    '%s': '%s',\n" % (rExpr, hexString(binRExpr)))
        fp.write("    }\n")
        r.close()
    finally:
        rProc.terminate()  # this call is only available in python2.6 and above


if __name__ == '__main__':
    createBinaryRExpressions()
else:
    try:
        import binaryRExpressions
    except ImportError:
        # it seems like the autogenerated module is not there yet. Create it, and then import it:
        print 'Cannot import binaryRExpressions, rebuilding them'
        createBinaryRExpressions()
        import binaryRExpressions
