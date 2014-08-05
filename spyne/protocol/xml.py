
#
# spyne - Copyright (C) Spyne contributors.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
#


"""The ``spyne.protocol.xml`` module contains an xml-based protocol that
serializes python objects to xml using Xml Schema conventions.

Logs valid documents to ``'%r'`` and invalid documents to ``'%r'``. Use the
usual ``logging.getLogger()`` and friends to configure how these get logged.

Warning! You can get a lot of crap in the 'invalid' logger. You're not advised
to turn it on for a production system.
""" % ('spyne.protocol.xml', 'spyne.protocol.xml.invalid')


import logging
logger = logging.getLogger('spyne.protocol.xml')
logger_invalid = logging.getLogger('spyne.protocol.xml.invalid')

from inspect import isgenerator
from collections import defaultdict

from lxml import etree
from lxml import html
from lxml.builder import E
from lxml.etree import XMLSyntaxError
from lxml.etree import XMLParser

from spyne import BODY_STYLE_WRAPPED

from spyne.util import _bytes_join, Break, coroutine
from spyne.util.six import text_type, string_types
from spyne.util.cdict import cdict
from spyne.util.etreeconv import etree_to_dict, dict_to_etree,\
    root_dict_to_etree

from spyne.error import Fault
from spyne.error import ValidationError
from spyne.const.ansi_color import LIGHT_GREEN
from spyne.const.ansi_color import LIGHT_RED
from spyne.const.ansi_color import END_COLOR
from spyne.const.xml_ns import xsi as _ns_xsi
from spyne.const.xml_ns import soap_env as _ns_soap_env
from spyne.const.xml_ns import const_prefmap, DEFAULT_NS
_pref_soap_env = const_prefmap[_ns_soap_env]

from spyne.model import ModelBase
from spyne.model import Array
from spyne.model import Iterable
from spyne.model import ComplexModelBase
from spyne.model import AnyHtml
from spyne.model import AnyXml
from spyne.model import AnyDict
from spyne.model import Unicode
from spyne.model import PushBase
from spyne.model import File
from spyne.model import ByteArray
from spyne.model import XmlData
from spyne.model import XmlAttribute
from spyne.model.binary import Attachment # deprecated
from spyne.model.binary import BINARY_ENCODING_BASE64
from spyne.model.enum import EnumBase

from spyne.protocol import ProtocolBase

NIL_ATTR = {'{%s}nil' % _ns_xsi: 'true'}


def _append(parent, child_elt):
    if hasattr(parent, 'append'):
        parent.append(child_elt)
    else:
        parent.write(child_elt)

def _gen_tagname(ns, name):
    if ns is not None:
        name = "{%s}%s" % (ns, name)
    return name

class SchemaValidationError(Fault):
    """Raised when the input stream could not be validated by the Xml Schema."""

    def __init__(self, faultstring):
        super(SchemaValidationError, self) \
                          .__init__('Client.SchemaValidationError', faultstring)


class SubXmlBase(ProtocolBase):
    def subserialize(self, ctx, cls, inst, parent, ns=None, name=None):
        return self.to_parent(ctx, cls, inst, parent, name)

    def to_parent(self, ctx, cls, inst, parent, ns, *args, **kwargs):
        """Serializes inst to an Element instance and appends it to the 'parent'.

        :param self:  The protocol that will be used to serialize the given
            value.
        :param cls:   The type of the value that's going to determine how to
            pack the given value.
        :param inst: The value to be set for the 'text' element of the newly
            created SubElement
        :param parent: The parent Element to which the new child will be
            appended.
        :param ns:   The target namespace of the new SubElement, used with
            'name' to set the tag.
        :param name:  The tag name of the new SubElement, 'retval' by default.
        """
        raise NotImplementedError()


class XmlDocument(SubXmlBase):
    """The Xml input and output protocol, using the information from the Xml
    Schema generated by Spyne types.

    See the following material for more (much much more!) information.

    * http://www.w3.org/TR/xmlschema-0/
    * http://www.w3.org/TR/xmlschema-1/
    * http://www.w3.org/TR/xmlschema-2/

    Receiving Xml from untrusted sources is a dodgy security dance as the Xml
    attack surface is /huge/.

    Spyne's ```lxml.etree.XMLParser``` instance has ```resolve_pis```,
    ```load_dtd```, ```resolve_entities```, ```dtd_validation```,
    ```huge_tree``` off by default.

    Having ```resolve_entities``` disabled will prevent the 'lxml' validation
    for documents with custom xml entities defined in the DTD. See the example
    in examples/xml/validation_error to play with the settings that work best
    for you. Please note that enabling ```resolve_entities``` is a security
    hazard that can lead to disclosure of sensitive information.

    See https://pypi.python.org/pypi/defusedxml for a pragmatic overview of
    Xml security in Python world.

    :param app: The owner application instance.
    :param validator: One of (None, 'soft', 'lxml', 'schema',
                ProtocolBase.SOFT_VALIDATION, XmlDocument.SCHEMA_VALIDATION).
                Both ``'lxml'`` and ``'schema'`` values are equivalent to
                ``XmlDocument.SCHEMA_VALIDATION``.
    :param xml_declaration: Whether to add xml_declaration to the responses
        Default is 'True'.
    :param cleanup_namespaces: Whether to add clean up namespace declarations
        in the response document. Default is 'True'.
    :param encoding: The suggested string encoding for the returned xml
        documents. The transport can override this.
    :param pretty_print: When ``True``, returns the document in a pretty-printed
        format.

    The following are passed straight to the XMLParser() instance. Docs are
    plagiarized from the lxml documentation. Please note that some of the
    defaults are different to make parsing safer by default.

    :param attribute_defaults: read the DTD (if referenced by the document) and
        add the default attributes from it. Off by default.
    :param dtd_validation: validate while parsing (if a DTD was referenced). Off
        by default.
    :param load_dtd: load and parse the DTD while parsing (no validation is
        performed). Off by default.
    :param no_network: prevent network access when looking up external
        documents. On by default.
    :param ns_clean: try to clean up redundant namespace declarations. Off by
        default. The note that this is for incoming documents. The
        ```cleanup_namespaces``` parameter is for output documents, which is
        that's on by default.
    :param recover: try hard to parse through broken Xml. Off by default.
    :param remove_blank_text: discard blank text nodes between tags, also known
        as ignorable whitespace. This is best used together with a DTD or schema
        (which tells data and noise apart), otherwise a heuristic will be
        applied. Off by default.
    :param remove_pis: discard processing instructions. On by default.
    :param strip_cdata: replace CDATA sections by normal text content. On by
        default.
    :param resolve_entities: replace entities by their text value. Off by
        default.
    :param huge_tree: disable security restrictions and support very deep trees
        and very long text content. (only affects libxml2 2.7+) Off by default.
    :param compact: use compact storage for short text content. On by default.
    """

    SCHEMA_VALIDATION = type("Schema", (object,), {})

    mime_type = 'text/xml'
    default_binary_encoding = BINARY_ENCODING_BASE64

    type = set(ProtocolBase.type)
    type.add('xml')

    def __init__(self, app=None, validator=None, xml_declaration=True,
                cleanup_namespaces=True, encoding=None, pretty_print=False,
                attribute_defaults=False,
                dtd_validation=False,
                load_dtd=False,
                no_network=True,
                ns_clean=False,
                recover=False,
                remove_blank_text=False,
                remove_pis=True,
                strip_cdata=True,
                resolve_entities=False,
                huge_tree=False,
                compact=True,
                binary_encoding=None,
            ):
        super(XmlDocument, self).__init__(app, validator,
                                                binary_encoding=binary_encoding)
        self.xml_declaration = xml_declaration
        self.cleanup_namespaces = cleanup_namespaces

        if encoding is None:
            self.encoding = 'UTF-8'
        else:
            self.encoding = encoding

        self.pretty_print = pretty_print

        self.serialization_handlers = cdict({
            AnyXml: self.xml_to_parent,
            Fault: self.fault_to_parent,
            AnyDict: self.dict_to_parent,
            AnyHtml: self.html_to_parent,
            EnumBase: self.enum_to_parent,
            XmlData: self.xmldata_to_parent,
            ModelBase: self.modelbase_to_parent,
            ByteArray: self.byte_array_to_parent,
            Attachment: self.attachment_to_parent,
            XmlAttribute: self.xmlattribute_to_parent,
            ComplexModelBase: self.complex_to_parent,
            SchemaValidationError: self.schema_validation_error_to_parent,
        })

        self.deserialization_handlers = cdict({
            AnyHtml: self.html_from_element,
            AnyXml: self.xml_from_element,
            Array: self.array_from_element,
            Fault: self.fault_from_element,
            AnyDict: self.dict_from_element,
            EnumBase: self.enum_from_element,
            ModelBase: self.base_from_element,
            Unicode: self.unicode_from_element,
            Iterable: self.iterable_from_element,
            ByteArray: self.byte_array_from_element,
            Attachment: self.attachment_from_element,
            ComplexModelBase: self.complex_from_element,
        })

        self.log_messages = (logger.level == logging.DEBUG)
        self.parser_kwargs = dict(
            attribute_defaults=attribute_defaults,
            dtd_validation=dtd_validation,
            load_dtd=load_dtd,
            no_network=no_network,
            ns_clean=ns_clean,
            recover=recover,
            remove_blank_text=remove_blank_text,
            remove_comments=True,
            remove_pis=remove_pis,
            strip_cdata=strip_cdata,
            resolve_entities=resolve_entities,
            huge_tree=huge_tree,
            compact=compact,
            encoding=encoding,
        )

    def subserialize(self, ctx, cls, inst, parent, ns=None, name=None):
        return self.to_parent(ctx, cls, inst, parent, name)

    def set_validator(self, validator):
        if validator in ('lxml', 'schema') or \
                                    validator is self.SCHEMA_VALIDATION:
            self.validate_document = self.__validate_lxml
            self.validator = self.SCHEMA_VALIDATION

        elif validator == 'soft' or validator is self.SOFT_VALIDATION:
            self.validator = self.SOFT_VALIDATION

        elif validator is None:
            pass

        else:
            raise ValueError(validator)

        self.validation_schema = None

    def validate_body(self, ctx, message):
        """Sets ctx.method_request_string and calls :func:`generate_contexts`
        for validation."""

        assert message in (self.REQUEST, self.RESPONSE), message

        line_header = LIGHT_RED + "Error:" + END_COLOR
        try:
            self.validate_document(ctx.in_body_doc)
            if message is self.REQUEST:
                line_header = LIGHT_GREEN + "Method request string:" + END_COLOR
            else:
                line_header = LIGHT_RED + "Response:" + END_COLOR
        finally:
            if self.log_messages:
                logger.debug("%s %s" % (line_header, ctx.method_request_string))
                logger.debug(etree.tostring(ctx.in_document, pretty_print=True))

    def set_app(self, value):
        ProtocolBase.set_app(self, value)

        self.validation_schema = None

        if value:
            from spyne.interface.xml_schema import XmlSchema

            xml_schema = XmlSchema(value.interface)
            xml_schema.build_validation_schema()

            self.validation_schema = xml_schema.validation_schema

    def __validate_lxml(self, payload):
        ret = self.validation_schema.validate(payload)

        logger.debug("Validated ? %r" % ret)
        if ret == False:
            error_text = text_type(self.validation_schema.error_log.last_error)
            raise SchemaValidationError(error_text.encode('ascii',
                                                           'xmlcharrefreplace'))

    def create_in_document(self, ctx, charset=None):
        """Uses the iterable of string fragments in ``ctx.in_string`` to set
        ``ctx.in_document``."""

        string = _bytes_join(ctx.in_string)
        try:
            try:
                ctx.in_document = etree.fromstring(string,
                                        parser=XMLParser(**self.parser_kwargs))

            except ValueError:
                logger.debug('ValueError: Deserializing from unicode strings '
                             'with encoding declaration is not supported by '
                             'lxml.')
                ctx.in_document = etree.fromstring(string.decode(charset),
                                                                    self.parser)
        except XMLSyntaxError as e:
            logger_invalid.error(string)
            raise Fault('Client.XMLSyntaxError', str(e))

    def decompose_incoming_envelope(self, ctx, message):
        assert message in (self.REQUEST, self.RESPONSE)

        ctx.in_header_doc = None # If you need header support, you should use Soap
        ctx.in_body_doc = ctx.in_document
        ctx.method_request_string = ctx.in_body_doc.tag
        self.validate_body(ctx, message)

    def from_element(self, ctx, cls, element):
        if bool(element.get('{%s}nil' % _ns_xsi)):
            if self.validator is self.SOFT_VALIDATION and not \
                                                        cls.Attributes.nillable:
                raise ValidationError('')
            return cls.Attributes.default

        handler = self.deserialization_handlers[cls]
        return handler(ctx, cls, element)

    def to_parent(self, ctx, cls, inst, parent, ns, *args, **kwargs):
        subprot = getattr(cls.Attributes, 'prot', None)
        if subprot is not None:
            return subprot.subserialize(ctx, cls, inst, parent, ns,
                                                                *args, **kwargs)

        handler = self.serialization_handlers[cls]

        if inst is None:
            inst = cls.Attributes.default

        if inst is None:
            return self.null_to_parent(ctx, cls, inst, parent, ns,
                                                                *args, **kwargs)
        return handler(ctx, cls, inst, parent, ns, *args, **kwargs)

    def deserialize(self, ctx, message):
        """Takes a MethodContext instance and a string containing ONE root xml
        tag.

        Returns the corresponding native python object.

        Not meant to be overridden.
        """

        assert message in (self.REQUEST, self.RESPONSE)

        self.event_manager.fire_event('before_deserialize', ctx)

        if ctx.descriptor is None:
            if ctx.in_error is None:
                raise Fault("Client", "Method %r not found." %
                                                      ctx.method_request_string)
            else:
                raise ctx.in_error

        if message is self.REQUEST:
            body_class = ctx.descriptor.in_message
        elif message is self.RESPONSE:
            body_class = ctx.descriptor.out_message

        # decode method arguments
        if ctx.in_body_doc is None:
            ctx.in_object = [None] * len(body_class._type_info)
        else:
            ctx.in_object = self.from_element(ctx, body_class, ctx.in_body_doc)

        if self.log_messages and message is self.REQUEST:
            line_header = '%sRequest%s' % (LIGHT_GREEN, END_COLOR)

            logger.debug("%s %s" % (line_header, etree.tostring(ctx.out_document,
                    xml_declaration=self.xml_declaration, pretty_print=True)))

        self.event_manager.fire_event('after_deserialize', ctx)

    def serialize(self, ctx, message):
        """Uses ``ctx.out_object``, ``ctx.out_header`` or ``ctx.out_error`` to
        set ``ctx.out_body_doc``, ``ctx.out_header_doc`` and
        ``ctx.out_document`` as an ``lxml.etree._Element instance``.

        Not meant to be overridden.
        """

        assert message in (self.REQUEST, self.RESPONSE)

        self.event_manager.fire_event('before_serialize', ctx)

        if ctx.out_error is not None:
            tmp_elt = etree.Element('punk')
            retval = self.to_parent(ctx, ctx.out_error.__class__, ctx.out_error,
                                    tmp_elt, self.app.interface.get_tns())
            ctx.out_document = tmp_elt[0]

        else:
            if message is self.REQUEST:
                result_message_class = ctx.descriptor.in_message
            elif message is self.RESPONSE:
                result_message_class = ctx.descriptor.out_message

            # assign raw result to its wrapper, result_message
            if ctx.descriptor.body_style == BODY_STYLE_WRAPPED:
                result_message = result_message_class()

                for i, attr_name in enumerate(
                                        result_message_class._type_info.keys()):
                    setattr(result_message, attr_name, ctx.out_object[i])

            else:
                result_message = ctx.out_object

            if ctx.out_stream is None:
                tmp_elt = etree.Element('punk')
                retval = self.to_parent(ctx, result_message_class,
                          result_message, tmp_elt, self.app.interface.get_tns())
                ctx.out_document = tmp_elt[0]

            else:
                retval = self.incgen(ctx, result_message_class,
                                  result_message, self.app.interface.get_tns())

        if self.cleanup_namespaces and ctx.out_document is not None:
            etree.cleanup_namespaces(ctx.out_document)

        self.event_manager.fire_event('after_serialize', ctx)

        return retval

    def create_out_string(self, ctx, charset=None):
        """Sets an iterable of string fragments to ctx.out_string"""

        if charset is None:
            charset = self.encoding

        ctx.out_string = [etree.tostring(ctx.out_document,
                                          encoding=charset,
                                          pretty_print=self.pretty_print,
                                          xml_declaration=self.xml_declaration)]

        if self.log_messages:
            logger.debug('%sResponse%s %s' % (LIGHT_RED, END_COLOR,
                            etree.tostring(ctx.out_document,
                                          pretty_print=True, encoding='UTF-8')))

    @coroutine
    def incgen(self, ctx, cls, inst, ns, name=None):
        if name is None:
            name = cls.get_type_name()
        with etree.xmlfile(ctx.out_stream) as xf:
            ret = self.to_parent(ctx, cls, inst, xf, ns, name)
            if isgenerator(ret):
                try:
                    while True:
                        y = (yield) # may throw Break
                        ret.send(y)

                except Break:
                    try:
                        ret.throw(Break())
                    except StopIteration:
                        pass

        if hasattr(ctx.out_stream, 'finish'):
            ctx.out_stream.finish()

    def byte_array_to_parent(self, ctx, cls, inst, parent, ns, name='retval'):
        _append(parent, E(_gen_tagname(ns, name),
                       self.to_string(cls, inst, self.binary_encoding)))

    def modelbase_to_parent(self, ctx, cls, inst, parent, ns, name='retval'):
        _append(parent, E(_gen_tagname(ns, name), self.to_string(cls, inst)))

    def null_to_parent(self, ctx, cls, inst, parent, ns, name='retval'):
        if issubclass(cls, XmlAttribute):
            return

        elif issubclass(cls, XmlData):
            parent.attrib.update(NIL_ATTR)

        else:
            _append(parent, E(_gen_tagname(ns, name), **NIL_ATTR))

    def null_from_element(self, ctx, cls, element):
        return None

    def xmldata_to_parent(self, ctx, cls, inst, parent, ns, name):
        ns = cls._ns
        if ns is None:
            ns = cls.Attributes.sub_ns

        name = _gen_tagname(ns, name)

        cls.marshall(self, name, inst, parent)

    def xmlattribute_to_parent(self, ctx, cls, inst, parent, ns, name):
        ns = cls._ns
        if ns is None:
            ns = cls.Attributes.sub_ns

        name = _gen_tagname(ns, name)

        if inst is not None:
            if issubclass(cls.type, (ByteArray, File)):
                parent.set(name, self.to_string(cls.type, inst,
                                                 self.binary_encoding))
            else:
                parent.set(name, self.to_string(cls.type, inst))

    def attachment_to_parent(self, cls, inst, ns, parent, name='retval'):
        _append(parent, E(_gen_tagname(ns, name),
                        ''.join([b.decode('ascii') for b in cls.to_base64(inst)])))

    @coroutine
    def gen_members_parent(self, ctx, cls, inst, parent, tag_name, subelts):
        delay = set()

        if isinstance(parent, etree._Element):
            elt = etree.SubElement(parent, tag_name)
            elt.extend(subelts)
            ret = self._get_members_etree(ctx, cls, inst, elt, delay)

            if isgenerator(ret):
                try:
                    while True:
                        y = (yield) # may throw Break
                        ret.send(y)

                except Break:
                    try:
                        ret.throw(Break())
                    except StopIteration:
                        pass

        else:
            with parent.element(tag_name):
                for e in subelts:
                    parent.write(e)
                ret = self._get_members_etree(ctx, cls, inst, parent, delay)
                if isgenerator(ret):
                    try:
                        while True:
                            y = (yield)
                            ret.send(y)

                    except Break:
                        try:
                            ret.throw(Break())
                        except StopIteration:
                            pass

    @coroutine
    def _get_members_etree(self, ctx, cls, inst, parent, delay):
        try:
            parent_cls = getattr(cls, '__extends__', None)

            if not (parent_cls is None):
                ret = self._get_members_etree(ctx, parent_cls, inst, parent, delay)
                if ret is not None:
                    try:
                        while True:
                            sv2 = (yield) # may throw Break
                            ret.send(sv2)

                    except Break:
                        try:
                            ret.throw(Break())
                        except StopIteration:
                            pass

            for k, v in cls._type_info.items():
                try:
                    subvalue = getattr(inst, k, None)
                except: # e.g. SqlAlchemy could throw NoSuchColumnError
                    subvalue = None

                # This is a tight loop, so enable this only when necessary.
                # logger.debug("get %r(%r) from %r: %r" % (k, v, inst, subvalue))

                sub_ns = v.Attributes.sub_ns
                if sub_ns is None:
                    sub_ns = cls.get_namespace()

                sub_name = v.Attributes.sub_name
                if sub_name is None:
                    sub_name = k

                if issubclass(v, XmlAttribute) and \
                                        v.attribute_of in cls._type_info.keys():
                    delay.add(k)
                    continue

                mo = v.Attributes.max_occurs
                if subvalue is not None and mo > 1:
                    if isinstance(subvalue, PushBase):
                        while True:
                            sv = (yield)
                            ret = self.to_parent(ctx, v, sv, parent, sub_ns,
                                                                       sub_name)
                            if ret is not None:
                                try:
                                    while True:
                                        sv2 = (yield) # may throw Break
                                        ret.send(sv2)

                                except Break:
                                    try:
                                        ret.throw(Break())
                                    except StopIteration:
                                        pass

                    else:
                        for sv in subvalue:
                            ret = self.to_parent(ctx, v, sv, parent, sub_ns,
                                                                       sub_name)

                            if ret is not None:
                                try:
                                    while True:
                                        sv2 = (yield) # may throw Break
                                        ret.send(sv2)

                                except Break:
                                    try:
                                        ret.throw(Break())
                                    except StopIteration:
                                        pass

                # Don't include empty values for non-nillable optional attributes.
                elif subvalue is not None or v.Attributes.min_occurs > 0:
                    ret = self.to_parent(ctx, v, subvalue, parent, sub_ns,
                                                                       sub_name)
                    if ret is not None:
                        try:
                            while True:
                                sv2 = (yield)
                                ret.send(sv2)
                        except Break as b:
                            try:
                                ret.throw(b)
                            except StopIteration:
                                pass

        except Break:
            pass

        if isinstance(parent, etree._Element):
            # attribute_of won't work with async.
            for k in delay:
                v = cls._type_info[k]

                subvalue = getattr(inst, k, None)
                sub_name = v.Attributes.sub_name
                if sub_name is None:
                    sub_name = k

                a_of = v.attribute_of
                ns = cls.__namespace__
                attr_parents = parent.findall("{%s}%s" % (ns, a_of))

                if cls._type_info[a_of].Attributes.max_occurs > 1:
                    for subsubvalue, attr_parent in zip(subvalue, attr_parents):
                        self.to_parent(ctx, v, subsubvalue, attr_parent,
                                                           v.get_namespace(), k)

                else:
                    for attr_parent in attr_parents:
                        self.to_parent(ctx, v, subvalue, attr_parent,
                                                           v.get_namespace(), k)


    def complex_to_parent(self, ctx, cls, inst, parent, ns, name=None):
        sub_name = cls.Attributes.sub_name
        if sub_name is not None:
            name = sub_name
        if name is None:
            name = cls.get_type_name()

        sub_ns = cls.Attributes.sub_ns
        if not sub_ns in (None, DEFAULT_NS):
            ns = sub_ns

        tag_name = _gen_tagname(ns, name)

        inst = cls.get_serialization_instance(inst)
        return self.gen_members_parent(ctx, cls, inst, parent, tag_name, [])

    def fault_to_parent(self, ctx, cls, inst, parent, ns, *args, **kwargs):
        tag_name = "{%s}Fault" % _ns_soap_env

        subelts = [
            E("faultcode", '%s:%s' % (_pref_soap_env, inst.faultcode)),
            E("faultstring", inst.faultstring),
            E("faultactor", inst.faultactor),
        ]

        # Accepting raw lxml objects as detail is deprecated. It's also not
        # documented. It's kept for backwards-compatibility purposes.
        if isinstance(inst.detail, string_types + (etree._Element,)):
            _append(subelts, E('detail', inst.detail))
        elif isinstance(inst.detail, dict):
            _append(subelts, E('detail', root_dict_to_etree(inst.detail)))
        elif inst.detail is None:
            pass
        else:
            raise TypeError('Fault detail Must be dict, got', type(inst.detail))

        # add other nonstandard fault subelements with get_members_etree
        return self.gen_members_parent(ctx, cls, inst, parent, tag_name, subelts)

    def schema_validation_error_to_parent(self, ctx, cls, inst, parent, ns):
        tag_name = "{%s}Fault" % _ns_soap_env

        subelts = [
            E("faultcode", '%s:%s' % (_pref_soap_env, inst.faultcode)),
            # HACK: Does anyone know a better way of injecting raw xml entities?
            E("faultstring", html.fromstring(inst.faultstring).text),
            E("faultactor", inst.faultactor),
        ]
        if inst.detail != None:
            _append(subelts, E('detail', inst.detail))

        # add other nonstandard fault subelements with get_members_etree
        return self.gen_members_parent(ctx, cls, inst, parent, tag_name, subelts)

    def enum_to_parent(self, ctx, cls, inst, parent, ns, name='retval'):
        self.modelbase_to_parent(ctx, cls, str(inst), parent, ns, name)

    def xml_to_parent(self, ctx, cls, inst, parent, ns, name):
        if isinstance(inst, str) or isinstance(inst, unicode):
            inst = etree.fromstring(inst)

        _append(parent, E(_gen_tagname(ns, name), inst))

    def html_to_parent(self, ctx, cls, inst, parent, ns, name):
        if isinstance(inst, string_types) and len(inst) > 0:
            inst = html.fromstring(inst)

        _append(parent, E(_gen_tagname(ns, name), inst))

    def dict_to_parent(self, ctx, cls, inst, parent, ns, name):
        elt = E(_gen_tagname(ns, name))
        dict_to_etree(inst, elt)

        _append(parent, elt)

    def complex_from_element(self, ctx, cls, elt):
        inst = cls.get_deserialization_instance()

        # if present, use the xsi:type="ns0:ObjectName"
        # attribute to instantiate subclass objects
        xsi_type = elt.get('{%s}type' % _ns_xsi)
        if xsi_type:
            orig_inst = inst
            orig_cls = cls
            try:
                prefix, objtype = xsi_type.split(':')
                classkey = xsi_type.replace("%s:" % prefix,
                                            "{%s}" % elt.nsmap[prefix])
                newclass = ctx.app.interface.classes[classkey]
                inst = newclass()
                cls = newclass
            except:
                # bail out and revert to original instance and class
                inst = orig_inst
                cls = orig_cls

        flat_type_info = cls.get_flat_type_info(cls)

        # this is for validating cls.Attributes.{min,max}_occurs
        frequencies = defaultdict(int)

        xtba_key, xtba_type = cls.Attributes._xml_tag_body_as
        if xtba_key is not None:
            if issubclass(xtba_type.type, (ByteArray, File)):
                value = self.from_string(xtba_type.type, elt.text,
                                                    self.binary_encoding)
            else:
                value = self.from_string(xtba_type.type, elt.text)
            setattr(inst, xtba_key, value)

        # parse input to set incoming data to related attributes.
        for c in elt:
            key = c.tag.split('}')[-1]
            frequencies[key] += 1

            member = flat_type_info.get(key, None)
            if member is None:
                member, key = cls._type_info_alt.get(key, (None, key))
                if member is None:
                    member, key = cls._type_info_alt.get(c.tag, (None, key))
                    if member is None:
                        continue

            mo = member.Attributes.max_occurs
            if mo > 1:
                value = getattr(inst, key, None)
                if value is None:
                    value = []

                value.append(self.from_element(ctx, member, c))

            else:
                value = self.from_element(ctx, member, c)

            setattr(inst, key, value)

            for key, value_str in c.attrib.items():
                member = flat_type_info.get(key, None)
                if member is None:
                    member, key = cls._type_info_alt.get(key, (None, key))
                    if member is None:
                        continue

                if (not issubclass(member, XmlAttribute)) or \
                                                         member.attribute_of == key:
                    continue

                if mo > 1:
                    value = getattr(inst, key, None)
                    if value is None:
                        value = []

                    value.append(self.from_string(member.type, value_str))

                else:
                    value = self.from_string(member.type, value_str)

                setattr(inst, key, value)

        for key, value_str in elt.attrib.items():
            member = flat_type_info.get(key, None)
            if member is None:
                member, key = cls._type_info_alt.get(key, (None, key))
                if member is None:
                    continue

            if (not issubclass(member, XmlAttribute)) or member.attribute_of == key:
                continue

            if issubclass(member.type, (ByteArray, File)):
                value = self.from_string(member.type, value_str,
                                                       self.binary_encoding)
            else:
                value = self.from_string(member.type, value_str)

            setattr(inst, key, value)

        if self.validator is self.SOFT_VALIDATION:
            for key, c in flat_type_info.items():
                val = frequencies.get(key, 0)
                attr = c.Attributes
                if val < attr.min_occurs or val > attr.max_occurs:
                    raise Fault('Client.ValidationError', '%r member does not '
                                         'respect frequency constraints.' % key)

        return inst

    def array_from_element(self, ctx, cls, element):
        retval = [ ]
        (serializer,) = cls._type_info.values()

        for child in element.getchildren():
            retval.append(self.from_element(ctx, serializer, child))

        return retval

    def iterable_from_element(self, ctx, cls, element):
        (serializer,) = cls._type_info.values()

        for child in element.getchildren():
            yield self.from_element(ctx, serializer, child)

    def enum_from_element(self, ctx, cls, element):
        if self.validator is self.SOFT_VALIDATION and not (
                                        cls.validate_string(cls, element.text)):
            raise ValidationError(element.text)
        return getattr(cls, element.text)

    def fault_from_element(self, ctx, cls, element):
        code = element.find('faultcode').text
        string = element.find('faultstring').text
        factor = element.find('faultactor')
        if factor is not None:
            factor = factor.text
        detail = element.find('detail')

        return cls(faultcode=code, faultstring=string, faultactor=factor,
                                                                  detail=detail)

    def xml_from_element(self, ctx, cls, element):
        children = element.getchildren()
        retval = None

        if children:
            retval = element.getchildren()[0]

        return retval

    def html_from_element(self, ctx, cls, element):
        children = element.getchildren()
        retval = None

        if len(children) == 1:
            retval = children[0]
        elif len(children) > 1:
            retval = E.p(*children)

        return retval

    def dict_from_element(self, ctx, cls, element):
        children = element.getchildren()
        if children:
            return etree_to_dict(element)

        return None

    def unicode_from_element(self, ctx, cls, element):
        if self.validator is self.SOFT_VALIDATION and not (
                                        cls.validate_string(cls, element.text)):
            raise ValidationError(element.text)

        s = element.text
        if s is None:
            s = ''

        retval = self.from_string(cls, s)

        if self.validator is self.SOFT_VALIDATION and not (
                                              cls.validate_native(cls, retval)):
            raise ValidationError(retval)

        return retval

    def base_from_element(self, ctx, cls, element):
        if self.validator is self.SOFT_VALIDATION and not (
                                        cls.validate_string(cls, element.text)):
            raise ValidationError(element.text)

        retval = self.from_string(cls, element.text)

        if self.validator is self.SOFT_VALIDATION and not (
                                            cls.validate_native(cls, retval)):
            raise ValidationError(retval)

        return retval

    def byte_array_from_element(self, ctx, cls, element):
        if self.validator is self.SOFT_VALIDATION and not (
                                        cls.validate_string(cls, element.text)):
            raise ValidationError(element.text)

        retval = self.from_string(cls, element.text, self.binary_encoding)

        if self.validator is self.SOFT_VALIDATION and not (
                                            cls.validate_native(cls, retval)):
            raise ValidationError(retval)

        return retval

    def attachment_from_element(self, ctx, cls, element):
        return cls.from_base64([element.text])
