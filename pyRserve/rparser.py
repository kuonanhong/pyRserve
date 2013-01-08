import struct, socket, io
###
from .rtypes import *
from .misc import FunctionMapper, byteEncode, stringEncode, PY3
from .rexceptions import RResponseError, REvalError
from .taggedContainers import TaggedList, asTaggedArray, asAttrArray

DEBUG = 0

class Lexeme(list):
    def __init__(self, rTypeCode, length, hasAttr, lexpos):
        list.__init__(self, [rTypeCode, length, hasAttr, lexpos])
        self.rTypeCode  = rTypeCode
        self.length     = length
        self.hasAttr    = hasAttr
        self.lexpos     = lexpos
        self.attrLexeme = None
        
    def setAttr(self, attrLexeme):
        self.attrLexeme = attrLexeme

    @property
    def attr(self):
        return self.attrLexeme.data if self.attrLexeme else None
        
    @property
    def attrLength(self):
        return self.attrLexeme.length
        
    @property
    def attrTypeCode(self):
        return self.attrLexeme.rTypeCode
        
    @property
    def dataLength(self):
        'Return length (in bytes) of actual REXPR data body'
        if self.hasAttr:
            if not self.attrLexeme:
                raise RuntimeError('Attribute lexeme not yet set')
            return self.length - self.attrLength - 4  # also subtract size of REXP header=4
        else:
            return self.length

    def __str__(self):
        return 'Typecode: %s   Length: %s  hasAttr: %s,  Lexpos: %d' % \
               (hex(self.rTypeCode), self.length, self.hasAttr, self.lexpos)



class EndOfDataError(RserveError):
    pass



class Lexer(object):
    #
    lexerMap = {}
    fmap = FunctionMapper(lexerMap)
    #
    def __init__(self, src):
        '''
        @param src: Either a string, a file object, a socket - all providing valid binary r data
        '''
        if type(src) == str:
            # this only works for objects implementing the buffer protocol, e.g. strings, arrays, ...
            self.fp = io.BytesIO(byteEncode(src))  # convert string to byte object
        elif type(src) == bytes:
            self.fp = io.BytesIO(src)
        else:
            self.fp = src
        if isinstance(self.fp, socket.socket):
            self._read = self.fp.recv
        else:
            self._read = self.fp.read
        
    def readHeader(self):
        '''
        Called initially when reading fresh data from an input source (file or socket). 
        Reads  header which contains data like response/error code and size of data entire package.
        '''
        self.lexpos = 0
        # First three bytes encode a 24bit response code, add an additional zero bytes and convert it:
        self.responseCode = struct.unpack(b'<i', self.read(3) + b'\x00')[0]
        if self.responseCode == RESP_OK:
            self.responseOK = True
        elif self.responseCode == RESP_ERR:
            self.responseOK = False
        else:
            self.clearSocketData()
            raise ValueError('Received illegal response code (%x)' % self.responseCode)
        self.errCode     = self.__unpack(XT_BYTE) 
        self.messageSize = self.__unpack(XT_INT)  
        self.read(8) # read additional 8 bytes from header -- CLEARIFY THIS!!
        if DEBUG:
            print('response ok? %s (responseCode=%x), error-code: %x, message size: %d' % \
                (self.responseOK, self.responseCode,  self.errCode,  self.messageSize))
        return self.messageSize

    def clearSocketData(self):
        '''
        If for any reason the parsing process returns an error, make sure that all data from
        a socket is removed to avoid data pollution with further parsing attempts.
        This should only be called after self.readHeader() has been executed.
        '''
        if type(self.fp) != socket.socket:  #hasattr(self.fp, '_sock'):
            # probably not a socket. Nothing to do here.
            return
        self.fp.setblocking(0)
        try:
            while 1:
                self.fp.recv(SOCKET_BLOCK_SIZE)
        except:
            pass
        finally:
            self.fp.setblocking(1)

    def read(self, length):
        '''
        Reads number of bytes from input data source (file or socket). If end of data is
        reached it raises EndOfDataError().

        Sockets might not return all requested data at once, so use an io buffer to collect all data needed in a loop.
        '''
        bytesToRead = length
        buf = io.BytesIO(b'')
        while bytesToRead > 0:
            fragment = self._read(bytesToRead)
            lenFrag = len(fragment)
            if lenFrag == 0:
                raise EndOfDataError()
            buf.write(fragment)
            bytesToRead -= lenFrag

        self.lexpos += length
        data = buf.getvalue()
        return data

    def __unpack(self, tCode, num=None):
        '''
        Reads 'num' (atomic) data items from the input source and converts them into a list 
        of python objects. Byteswapping for numeric data will be done.
        '''
        structCode = structMap[tCode] if type(tCode)==int else tCode
        # All data from Rserve is stored in little-endian format!
        fmt = byteEncode('<' + str(num) + structCode if (num is not None) else '<' + structCode)  # convert into bytes!
        if tCode == XT_INT3:
            length = 3
            rawData = self.read(length) + b'\x00'
        elif tCode == XT_INT7:
            length = 7
            rawData = self.read(length) + b'\x00'
        else:
            length = struct.calcsize(fmt or 1)
            rawData = self.read(length)
        d = struct.unpack(fmt, rawData)
        return d[0] if num is None else list(d)
    
    def nextExprHdr(self):
        '''
        From the input file/socket determine the type of the next data item, and its length.
        This method can be applied to read the
        - entire data header (containing one of the DT_* codes)
        - an REXPR header
        '''
        startLexpos = self.lexpos
        _rTypeCode  = self.__unpack('B')  # unsigned byte!
        rTypeCode   =  _rTypeCode & 0x3F                 # extract pure rTypeCode without XT_HAS_ATTR or XT_LARGE flags
        hasAttr     = (_rTypeCode & XT_HAS_ATTR) != 0    # extract XT_HAS_ATTR flag (if it exists)
        isXtLarge   = (_rTypeCode & XT_LARGE)    != 0    # extract XT_LARGE flag (if it exists)
        if isXtLarge:
            # header is larger, use all 7 bytes for length information (new in Rserve 0.3)
            length  = self.__unpack(XT_INT7)
        else:
            # small header, use 3 bytes for length information
            length  = self.__unpack(XT_INT3)
        if rTypeCode not in VALID_R_TYPES:
            raise RParserError("Unknown SEXP type %s found at lexpos %d, length %d" %
                               (hex(rTypeCode), startLexpos, length))
        return Lexeme(rTypeCode, length, hasAttr, startLexpos)
        
    def nextExprData(self, lexeme):
        '''
        Reads next data item from binary r data and transforms it into a python object.
        '''
        return self.lexerMap[lexeme.rTypeCode](self, lexeme)
            
    ####################################################################################
       
    @fmap(XT_INT, XT_DOUBLE)
    def xt_atom(self, lexeme):
        raw = self.read(lexeme.dataLength)
        return struct.unpack(byteEncode('<%s' % structMap[lexeme.rTypeCode]), raw)[0]

    @fmap(XT_BOOL)
    def xt_bool(self, lexeme):
        raw = self.read(lexeme.dataLength)
        # a boolean is stored in a 4 bytes word, but only the first byte is significant:
        if PY3:
            b = raw[0]  # python3 directly converts a single byte item into a number!
        else:
            b = struct.unpack(byteEncode('<%s' % structMap[XT_BOOL]), raw[0])[0]
        # b can be 2, meaning NA. Otherwise transform 0/1 into False/True
        return None if b==2 else b==1

    @fmap(XT_ARRAY_INT, XT_ARRAY_DOUBLE, XT_ARRAY_CPLX)
    def xt_array_numeric(self, lexeme):
        raw = self.read(lexeme.dataLength)
        # TODO: swapping...
        data = numpy.fromstring(raw, dtype=numpyMap[lexeme.rTypeCode])
        return data

    @fmap(XT_ARRAY_BOOL)
    def xt_array_bool(self, lexeme):
        """A boolean array consists of a 4-byte word (i.e. integer) determining the number of boolean values
        in the following dataLength-4 bytes.
        E.g. a bool array of one TRUE item looks like:
        01 00 00 00   01 ff ff ff

        The first 01 value tells that there is one bool value in the array.
        The other 01 is the TRUE value, the other 3 'ff's are padding bytes. Those will be used if the vector
        has 2,3 or 4 boolean values. For a fifth value another 4 bytes are appended.
        """
        numBools = self.__unpack(XT_INT, 1)[0]
        raw = self.read(lexeme.dataLength-4)  # read the actual boolean values, including padding bytes
        data = numpy.fromstring(raw[:numBools], dtype=numpyMap[lexeme.rTypeCode])
        return data

    @fmap(XT_ARRAY_STR)
    def xt_array_str(self, lexeme):
        '''
        An array of one or more null-terminated strings. 
        The XT_ARRAY_STR can contain trailing chars \x01 which need to be chopped off.
        Since strings are encoded as bytes (in Py3) they need to be converted into real strings.
        '''
        if lexeme.dataLength == 0:
            return ''
        raw = self.read(lexeme.dataLength)
        bytesStrList = raw.split(b'\0')[:-1]
        strList = [stringEncode(byteString) for byteString in bytesStrList]
        return numpy.array(strList)
        
    @fmap(XT_STR, XT_SYMNAME)
    def xt_symname(self, lexeme):
        '''
        A null-terminated string. 
        It's length can be larger than the actual string since it is always a multiple of 4.
        The rest is filled with trailing \0s which need to be chopped off.
        '''
        raw = self.read(lexeme.dataLength)
        byteStr = raw.split(b'\0', 1)[0]
        return stringEncode(byteStr)

    @fmap(XT_NULL)
    def xt_null(self, lexeme):
        return None

    @fmap(XT_UNKNOWN)
    def xt_unknown(self, lexeme):
        return self.__unpack(XT_INT)

    @fmap(XT_RAW)
    def xt_raw(self, lexeme):
        self.__unpack(XT_INT)
        return self.read(lexeme.dataLength - 4)


class RParser(object):
    #
    parserMap = {}
    fmap = FunctionMapper(parserMap)
    #
    def __init__(self, src, atomicArray):
        '''
        atomicArray: if False parsing arrays with only one element will just return this element
        arrayOrder:  The order in which data in multi-dimensional arrays is returned. 'C' for c-order, F for fortran.
        '''
        self.lexer = Lexer(src)
        self.atomicArray = atomicArray

    def __getitem__(self, key):
        return self.parserMap[key]

    def __getattr__(self, attr):
        if attr in ['messageSize']:
            return getattr(self.lexer, attr)
        else:
            raise AttributeError(attr)
            
    @property
    def __ind(self):
        # return string with number of spaces appropriate for current indentation level
        return self.indentLevel*4*' '
        
    def _debugLog(self, lexeme, isRexpr=True):
        if DEBUG:
            l = lexeme
            typeCodeDict = XTs if isRexpr else DTs
            print('%s %s (%s), hasAttr=%s, lexpos=%d, length=%s' %
                  (self.__ind, typeCodeDict[l.rTypeCode], hex(l.rTypeCode), l.hasAttr, l.lexpos, l.length))

    def parse(self):
        '''
        @brief parse data stream and return result converted into python data structure
        '''
        self.indentLevel = 1
        self.lexer.readHeader()
        if self.lexer.messageSize > 0:
            try:
                return self._parse()
            except:
                # If any error is raised during lexing and parsing, make sure that the entire data
                # is read from the input source if it is a socket, otherwise following attempts to 
                # parse again from a socket will return polluted data:
                self.lexer.clearSocketData()
                raise
        elif not self.lexer.responseOK:
            try:
                rserve_err_msg = ERRORS[self.lexer.errCode]
            except KeyError:
                raise REvalError("R evaluation error (code=%d)" % self.lexer.errCode)
            else:
                raise RResponseError('Response error %s (error code=%d)' % (rserve_err_msg, self.lexer.errCode))
            
    def _parse(self):
        dataLexeme = self.lexer.nextExprHdr()
        self._debugLog(dataLexeme, isRexpr=False)
        if dataLexeme.rTypeCode == DT_SEXP:
            expression = self._parseExpr()
            return self._postprocessData(expression.data)
        else:
            raise NotImplementedError()

    def _parseExpr(self):
        self.indentLevel += 1
        lexeme = self.lexer.nextExprHdr()
        self._debugLog(lexeme)
        if lexeme.hasAttr:
            self.indentLevel += 1
            if DEBUG:
                print('%s Attribute:' % self.__ind)
            lexeme.setAttr(self._parseExpr())
            self.indentLevel -= 1
        #import pdb;pdb.set_trace()
        lexeme.data = self.parserMap.get(lexeme.rTypeCode, self[None])(self, lexeme)
        self.indentLevel -= 1
        return lexeme
        
    def _nextExprData(self, lexeme):
        lexpos = self.lexer.lexpos
        data = self.lexer.nextExprData(lexeme)
        if DEBUG:
            print('%s    data-lexpos: %d, data-length: %d bytes' % (self.__ind, lexpos, lexeme.dataLength))
            print('%s    data: %s' % (self.__ind, repr(data)))
            try:
                dataLen = len(data)
                print('%s    length: %d' % (self.__ind, dataLen))
            except TypeError:
                pass
        return data
        
    def _postprocessData(self, data):
        """Postprocess parsing results depending on configuration parameters
        Currently only arrays are effected.
        """
        if data.__class__ == numpy.ndarray:
            # this does not apply for arrays with attributes (__class__ would be TaggedArray)!
            if len(data) == 1 and not self.atomicArray:
                # if data is a plain numpy array, and has only one element, just extract and return this.
                # For convenience reasons type-convert it into a native Python data type:
                data = data[0]
                if isinstance(data, (numpy.float, numpy.float64)):
                    data = float(data)    # convert into native python float
                elif isinstance(data, (numpy.int, numpy.int32, numpy.int64)):
                    data = int(data)      # convert into native int or long, depending on value
                elif isinstance(data, (numpy.complex, numpy.complex64, numpy.complex128)):
                    data = complex(data)  # convert into native python complex number
                elif isinstance(data, (numpy.string_, numpy.str_)):
                    data = str(data)      # convert into native python string
                elif isinstance(data, (numpy.bool_, numpy.bool, numpy.bool8)):
                    data = bool(data)     # convert into native python string
        return data

    @fmap(None)
    def xt_(self, lexeme):
        'apply this for atomic data'
        return self._nextExprData(lexeme)

    @fmap(XT_ARRAY_BOOL, XT_ARRAY_INT, XT_ARRAY_DOUBLE, XT_ARRAY_STR)
    def xt_array(self, lexeme):
        data = self._nextExprData(lexeme)  # converts data into a numpy array already
        if lexeme.hasAttr and lexeme.attrTypeCode == XT_LIST_TAG:
            for tag, value in lexeme.attr:
                if tag == 'dim':
                    # the array has a defined shape, and R stores and sends arrays in Fortran mode:
                    data = data.reshape(value, order='F')
                elif tag == 'names':
                    # convert numpy-vector 'value' into list to make TaggedArray work properly:
                    data = asTaggedArray(data, list(value))
                else:
                    # there are additional tags in the attribute, just collect them in a dictionary
                    # attached to the array. 
                    try:
                        data.attr[tag] = value
                    except AttributeError:
                        data = asAttrArray(data, {tag: value})
        return data

    @fmap(XT_VECTOR, XT_LANG_NOTAG, XT_LIST_NOTAG)
    def xt_vector(self, lexeme):
        '''
        A vector is e.g. return when sending "list('abc','def')" to R. It can contain mixed
        types of data items.
        The binary representation of an XT_VECTOR is weird: a vector contains unknown number 
        of items, with possibly variable length. Only the number of bytes of the data of a vector
        is known in advance.
        The end of this REXP can only be detected by keeping track of how many bytes
        have been consumed (lexeme.length!) until the end of the REXP has been reached.
        '''
        finalLexpos = self.lexer.lexpos + lexeme.dataLength
        if DEBUG:
            print('%s     Vector-lexpos: %d, length %d, finished at: %d' % \
                (self.__ind, self.lexer.lexpos, lexeme.dataLength, finalLexpos))
        data = []
        while self.lexer.lexpos < finalLexpos:
            # convert single item arrays into atoms (via stripArray)
            data.append(self._postprocessData(self._parseExpr().data))
            
        if lexeme.hasAttr and lexeme.attrTypeCode == XT_LIST_TAG:
            # The vector is actually a tagge list, i.e. a list which allows to access its items
            # by name (like in a dictionary). However items are ordered, and there is not
            # necessarily a name available for every item.
            for tag, value in lexeme.attr:
                if tag == 'names':
                    # the vector has named items
                    data = TaggedList(zip(value, data))
                else:
                    if DEBUG:
                        print('Warning: applying LIST_TAG "%s" on xt_vector not yet implemented' % tag)
        return data

    @fmap(XT_LIST_TAG, XT_LANG_TAG)
    def xt_list_tag(self, lexeme):
        # a xt_list_tag usually occurs as an attribute of a vector or list (like for a tagged list)
        finalLexpos = self.lexer.lexpos + lexeme.dataLength
        r = []
        while self.lexer.lexpos < finalLexpos:
            value, tag = self._parseExpr().data, self._parseExpr().data
            # reverse order of tag and value when adding it to result list
            r.append((tag, value))
        return r

    @fmap(XT_CLOS)
    def xt_closure(self, lexeme):
        # read entire data provided for closure (a R code object) even though we don't know
        # what to do with it on the Python side ;-)
        aList1 = self._parseExpr().data
        aList2 = self._parseExpr().data
        # Some closures seem to provide their sourcecode in an attrLexeme, but some don't.
        # return Closure(lexeme.attrLexeme.data[0][1])
        # So for now let's just return the entire parse tree in a Closure instance.
        return Closure(lexeme, aList1, aList2)



########################################################################################

def rparse(src, atomicArray=False):
    rparser = RParser(src, atomicArray)
    return rparser.parse()

########################################################################################

class Closure(object):
    'Very simple container to return "something" for a closure. Not really usable in Python though.'
    def __init__(self, lexeme, aList1, aList2):
        self.lexeme = lexeme
        self.aList1 = aList1
        self.aList2 = aList2
        
    def __repr__(self):
        return '<Closure instance %d>' % id(self)
