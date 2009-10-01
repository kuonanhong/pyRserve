import cStringIO, struct, collections
###
from rtypes import *
from misc import FunctionMapper

DEBUG = True

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
        try:
            # this only works for objects implementing the buffer protocol, e.g. strings, arrays, ...
            self.fp = cStringIO.StringIO(src) 
        except TypeError:
            self.fp = src
        
    def readHeader(self):
        '''
        Called initially when reading fresh data from an input source (file or socket). 
        Reads  header which contains data like response/error code and size of data entire package.
        '''
        self.lexpos = 0
        # First three bytes encode a 24bit response code, add an additional zero bytes and convert it:
        self.responseCode = struct.unpack('<i', self.read(3) + '\x00')[0]
        if self.responseCode == RESP_OK:
            self.responseOK = True
        elif self.responseCode == RESP_ERR:
            self.responseOK = False
        else:
            raise ValueError('Received illegal response code (%x)' % self.responseCode)
        self.errCode     = self.__unpack(XT_BYTE) 
        self.messageSize = self.__unpack(XT_INT)  
        self.read(8) # read additional 8 bytes from header -- CLEARIFY THIS!!
        if DEBUG:
            print 'response ok? %s (responseCode=%x), error-code: %x, message size: %d' % \
                (self.responseOK, self.responseCode,  self.errCode,  self.messageSize)
                  
    def clearSocketData(self):
        '''
        If for any reason the parsing process returns an error, make sure that all data from
        a socket is removed to avoid data pollution with further parsing attempts.
        This should only be called after self.readHeader() has been executed.
        '''
        if not hasattr(self.fp, '_sock'):
            # probably not a socket. Nothing to do here.
            return
        self.fp._sock.setblocking(0)
        try:
            while 1:
                self.fp.read(SOCKET_BLOCK_SIZE)
        except:
            pass
        finally:
            self.fp._sock.setblocking(1)

    def read(self, length):
        '''
        Reads number of bytes from input data source (file or socket). If end of data is
        reached it raises EndOfDataError().
        '''
        if length==0:
            # this can happen if an empty string is read from the data source
            data = ''
        else:
            self.lexpos += length
            data = self.fp.read(length)
            if len(data) == 0:
                raise EndOfDataError()
        return data

    def __unpack(self, tCode, num=None):
        '''
        Reads 'num' (atomic) data items from the input source and converts them into a list 
        of python objects. Byteswapping for numeric data will be done.
        '''
        structCode = structMap[tCode] if type(tCode)==int else tCode
        fmt = '<' + str(num) + structCode if (num is not None) else '<' + structCode
        if tCode == XT_INT3:
            length = 3
            rawData = self.read(length) + '\x00'
        else:
            length = struct.calcsize(fmt or 1)
            rawData = self.read(length)
        d = struct.unpack(fmt, rawData)
        return d[0] if num is None else list(d)
    
    def __unpackNullTerminatedStrings(self, num):
        '''
        @brief  Read 'num' null-terminated strings (i.e. q symbols) from the input source 
                (a file or a socket). 
        @note   All strings will be "interned"
        @return list (of strings)
        '''
        # there is an optimized way to read from a socket via lookahead, so differentiate 
        # these two cases
        try:
            sock = self.fp._sock
        except AttributeError:
            # read from a normal file or stream:
            data = readNullTermStringsFromStream(self, num)
        else:
            # read from a socket
            numBytesRead, data = readNullTerminatedStringsFromSock(sock, num)
            self.lexpos += numBytesRead
        # Make strings "intern", i.e. Python only keeps a single copy of it in memory and 
        # just returns new references to it. This is useful if a single symbol is used
        # thousands of times.
        return map(intern, data)

    def nextExprHdr(self):
        '''
        From the input file/socket determine the type of the next data item, and its length.
        This method can be applied to read the
        - entire data header (containing one of the DT_* codes)
        - an REXPR header
        '''
        startLexpos = self.lexpos
        _rTypeCode  = self.__unpack('B') # unsigned byte!
        rTypeCode   =  _rTypeCode & (0xFF - XT_HAS_ATTR)   # remove XT_HAS_ATTR flag (if it exists)
        hasAttr     = (_rTypeCode & XT_HAS_ATTR) != 0      # extract XT_HAS_ATTR flag (if it exists)
        length      = self.__unpack(XT_INT3)
        if not rTypeCode in VALID_R_TYPES:
            raise RParserError("Invalid token %s found at lexpos %d" % (hex(rTypeCode), startLexpos))
        return Lexeme(rTypeCode, length, hasAttr, startLexpos)
        
    def nextExprData(self, lexeme):
        '''
        Reads next data item from binary r data and transforms it into a python object.
        '''
        return self.lexerMap[lexeme.rTypeCode](self, lexeme)
            
        
    ###
       
    @fmap(XT_BOOL, XT_BYTE, XT_INT, XT_DOUBLE)
    def xt_atom(self, lexeme):
        raw = self.read(lexeme.dataLength)
        return struct.unpack('<%s' % structMap[lexeme.rTypeCode], raw)

    @fmap(XT_ARRAY_BOOL, XT_ARRAY_INT, XT_ARRAY_DOUBLE)
    def xt_array_numeric(self, lexeme):
        raw = self.read(lexeme.dataLength)
        # TODO: swapping...
        data = numpy.fromstring(raw, dtype=numpyMap[lexeme.rTypeCode])
        # The next needs to be discussed: In R everything is an array, how do we handle
        # singular array items in Python? Always as an array, or as an atomic item?
        if len(data) == 1:
            return data[0]
        else:
            return data

    @fmap(XT_ARRAY_STR)
    def xt_array_str(self, lexeme):
        '''
        An array of one or more null-terminated strings. 
        The XT_ARRAY_STR can contain trailing chars \x01 which need to be chopped off.
        '''
#        if lexeme.dataLength == 0:
#            return ''
#        raw = self.read(lexeme.dataLength)
#        data = raw.split('\0')[:-1]
#        return data if len(data) > 1 else data[0]
        raw = self.read(lexeme.dataLength)
        data = raw.split('\0')[:-1]
        return numpy.array(data)
        
    @fmap(XT_SYMNAME)
    def xt_symname(self, lexeme):
        '''
        A null-terminated string. 
        It's length an be larger than the actual string, it is always a multiple of 4.
        The rest is filled with trailing \0s which need to be chopped off.
        '''
        raw = self.read(lexeme.dataLength)
        return raw.split('\0')[0]

    @fmap(XT_NULL)
    def xt_null(self, lexeme):
        return None

    @fmap(XT_UNKNOWN)
    def xt_unknown(self, lexeme):
        return self.__unpack(XT_INT)


class RParser(object):
    #
    parserMap = {}
    fmap = FunctionMapper(parserMap)
    #
    def __init__(self, src):
        self.lexer = Lexer(src)

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
            print '%s %s (%s), hasAttr=%s, lexpos=%d, length=%s' % \
                  (self.__ind, typeCodeDict[l.rTypeCode], hex(l.rTypeCode),
                   l.hasAttr, l.lexpos, l.length)

    def parse(self):
        '''
        @brief parse data stream and return result converted into python data structure
        '''
        self.indentLevel = 1
        self.lexer.readHeader()
        try:
            return self._parse()
        except:
            # If any error is raised during lexing and parsing, make sure that the entire data
            # is read from the input source if it is a socket, otherwise following attempts to 
            # parse again from a socket will return polluted data:
            self.lexer.clearSocketData()
            raise
            
    def _parse(self):
        dataLexeme = self.lexer.nextExprHdr()
        self._debugLog(dataLexeme, isRexpr=False)
        if dataLexeme.rTypeCode == DT_SEXP:
            return self._parseExpr().data
        else:
            raise NotImplementedError()

    def _parseExpr(self):
        self.indentLevel += 1
        lexeme = self.lexer.nextExprHdr()
        self._debugLog(lexeme)
        if lexeme.hasAttr:
            self.indentLevel += 1
            if DEBUG:
                print '%s Attribute:' % (self.__ind)
            lexeme.setAttr(self._parseExpr())
            self.indentLevel -= 1
        lexeme.data = self.parserMap.get(lexeme.rTypeCode, self[None])(self, lexeme)
        self.indentLevel -= 1
        return lexeme
        
    def _nextExprData(self, lexeme):
        lexpos = self.lexer.lexpos
        data = self.lexer.nextExprData(lexeme)
        if DEBUG:
            print '%s    data-lexpos: %d, data-length: %d' % \
                    (self.__ind, lexpos, lexeme.dataLength)
            print '%s    data: %s' % (self.__ind, data)
        return data
        
    @fmap(None)
    def xt_(self, lexeme):
        'apply this for atomic data and arrays.'
        data = self._nextExprData(lexeme)
        if lexeme.hasAttr and lexeme.attrTypeCode == XT_LIST_TAG:
            for tag, value in lexeme.attr:
                if tag == 'dim':
                    # the array has a defined shape
                    data.shape = value
                elif tag == 'names':
                    data = asTaggedArray(data, value)
                elif tag in ['dimnames', 'assign']:
                    print 'Warning: applying LIST_TAG "%s" on array not yet implemented' % tag
                else:
                    raise NotImplementedError('cannot apply tag "%s" on array' % tag)
        return data

    @fmap(XT_VECTOR, XT_LANG_NOTAG, XT_LIST_NOTAG)
    def xt_vector(self, lexeme):
        '''
        This is a bit weird: a vector contains unknown number of items, with possibly variable
        length. The end of this REXP can only be detected by keeping track of how many bytes
        have been consumed (lexeme.length!) until the end of the REXP has been reached.
        '''
        finalLexpos = self.lexer.lexpos + lexeme.dataLength
        if DEBUG:
            print '%s Vector-data: starting at %s, length %d, finished at: %d' % \
                (self.__ind, self.lexer.lexpos, lexeme.dataLength, finalLexpos)
        data = []
        while self.lexer.lexpos < finalLexpos:
            data.append(self._parseExpr().data)
            
        if lexeme.hasAttr and lexeme.attrTypeCode == XT_LIST_TAG:
            for tag, value in lexeme.attr:
                if tag == 'names':
                    # the vector has named items
                    data = TaggedList(value, data)
                elif tag == 'class':
                    print 'Warning: applying LIST_TAG "%s" on xt_vector not yet implemented' % tag
        return data

    @fmap(XT_LIST_TAG, XT_LANG_TAG)
    def xt_list_tag(self, lexeme):
        finalLexpos = self.lexer.lexpos + lexeme.dataLength
        r = []
        while self.lexer.lexpos < finalLexpos:
            value, tag = self._parseExpr().data, self._parseExpr().data
            # reverse order of tag and value when adding it to result list
            r.append((tag, value))
        return r

    @fmap(XT_CLOS)
    def xt_closure(self, lexeme):
        aList1 = self._parseExpr().data
        print 'closure->list1', aList1
        #import pdb;pdb.set_trace()
        aList2 = self._parseExpr().data
        print 'closure->list2', aList2
        return (aList1, aList2)


########################################################################################

def rparse(src):
    rparser = RParser(src)
    return rparser.parse()


def TaggedList(titles, values):
    R = collections.namedtuple('TaggedList', [t.replace('.','_') for t in titles])
    return R(*values)

########################################################################################

class AttrArray(numpy.ndarray):
    'numpy.ndarray with additional "attr"-attribute'
    attr = None
    
def asAttrArray(ndarray, attr):
    arr = ndarray.view(AttrArray)
    arr.attr = attr
    return attr
    
class TaggedArray(AttrArray):
    attr = []
    def __getitem__(self, idx_or_name):
        try:
            return numpy.ndarray.__getitem__(self, idx_or_name)
        except:
            return numpy.ndarray.__getitem__(self, self.attr.index(idx_or_name))

def asTaggedArray(ndarray, tags):
    arr = ndarray.view(TaggedArray)
    arr.attr = tags
    return arr
    
