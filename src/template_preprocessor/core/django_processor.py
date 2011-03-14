#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Django template preprocessor.
Author: Jonathan Slenders, City Live
"""

"""
Django parser for a template preprocessor.
------------------------------------------------------------------
Parses django template tags.
This parser will call the html/css/js parser if required.
"""

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.utils.translation import ugettext as _
from django.core.urlresolvers import NoReverseMatch
from django.core.urlresolvers import reverse

from template_preprocessor.core.lexer import Token, State, StartToken, Shift, StopToken, Push, Pop, Error, Record, CompileException
from template_preprocessor.core.preprocessable_template_tags import PREPROCESS_TAGS, NotPreprocessable
from template_preprocessor.core.lexer_engine import nest_block_level_elements, tokenize
import re
from copy import deepcopy



__DJANGO_STATES = {
    'root' : State(
            # Start of django tag
            State.Transition(r'\{#', (StartToken('django-comment'), Shift(), Push('django-comment'))),
            State.Transition(r'\{%\s*comment\s*%\}', (StartToken('django-multiline-comment'), Shift(), Push('django-multiline-comment'))),
            State.Transition(r'\{%\s*', (StartToken('django-tag'), Shift(), Push('django-tag'))),
            State.Transition(r'\{{\s*', (StartToken('django-variable'), Shift(), Push('django-variable'))),

            # Content
            State.Transition(r'([^{]|%|{(?![%#{]))+', (StartToken('content'), Record(), Shift(), StopToken())),

            State.Transition(r'.|\s', (Error('Error in parser'),)),
        ),
    # {# .... #}
    'django-comment': State(
            State.Transition(r'#\}', (StopToken(), Shift(), Pop())),
            State.Transition(r'[^\n#]+', (Record(), Shift())),
            State.Transition(r'\n', (Error('No newlines allowed in django single line comment'), )),
            State.Transition(r'#(?!\})', (Record(), Shift())),

            State.Transition(r'.|\s', (Error('Error in parser: comment'),)),
        ),
    'django-multiline-comment': State(
            State.Transition(r'\{%\s*endcomment\s*%\}', (StopToken(), Shift(), Pop())), # {% endcomment %}
                    # Nested single line comments are allowed
            State.Transition(r'\{#', (StartToken('django-comment'), Shift(), Push('django-comment'))),
            State.Transition(r'[^{]+', (Record(), Shift(), )), # Everything except '{'
            State.Transition(r'\{(?!\{%\s*endcomment\s*%\}|#)', (Record(), Shift(), )), # '{' if not followed by '%endcomment%}'
        ),
    # {% tagname ... %}
    'django-tag': State(
            #State.Transition(r'([a-zA-Z0-9_\-\.|=:\[\]<>(),]+|"[^"]*"|\'[^\']*\')+', # Whole token as one
            State.Transition(r'([^\'"\s%}]+|"[^"]*"|\'[^\']*\')+', # Whole token as one
                                        (StartToken('django-tag-element'), Record(), Shift(), StopToken() )),
            State.Transition(r'\s*%\}', (StopToken(), Shift(), Pop())),
            State.Transition(r'\s+', (Shift(), )), # Skip whitespace

            State.Transition(r'.|\s', (Error('Error in parser: django-tag'),)),
        ),
    # {{ variable }}
    'django-variable': State(
            #State.Transition(r'([a-zA-Z0-9_\-\.|=:\[\]<>(),]+|"[^"]*"|\'[^\']*\')+',
            State.Transition(r'([^\'"\s%}]+|"[^"]*"|\'[^\']*\')+',
                                        (StartToken('django-variable-part'), Record(), Shift(), StopToken() )),
            State.Transition(r'\s*\}\}', (StopToken(), Shift(), Pop())),
            State.Transition(r'\s+', (Shift(), )),

            State.Transition(r'.|\s', (Error('Error in parser: django-variable'),)),
        ),
    }



class DjangoContainer(Token):
    """
    Any node which can contain both other Django nodes and DjangoContent.
    """
    pass

class DjangoContent(Token):
    """
    Any literal string to output. (html, javascript, ...)
    """
    pass


# ====================================[ Parser classes ]=====================================


class DjangoRootNode(DjangoContainer):
    """
    Root node of the parse tree.
    """
    pass

class DjangoComment(Token):
    """
    {# ... #}
    """
    def output(self, handler):
        # Don't output anything. :)
        pass

class DjangoMultilineComment(Token):
    """
    {% comment %} ... {% endcomment %}
    """
    def output(self, handler):
        # Don't output anything.
        pass

class DjangoTag(Token):
    @property
    def tagname(self):
        """
        return the tagname in: {% tagname option option|filter ... %}
        """
        # This is the first django-tag-element child
        for c in self.children:
            if c.name == 'django-tag-element':
                return c.output_as_string()

    def _args(self):
        for c in [c for c in self.children if c.name == 'django-tag-element'][1:]:
            yield c.output_as_string()

    @property
    def args(self):
        return list(self._args())

    def output(self, handler):
        handler(u'{%')
        for c in self.children:
            handler(c)
            handler(u' ')
        handler(u'%}')


class DjangoVariable(Token):
    def init_extension(self):
        self.__varname = Token.output_as_string(self, True)

    @property
    def varname(self):
        return self.__varname

    def output(self, handler):
        handler(u'{{')
        handler(self.__varname)
        handler(u'}}')


class DjangoPreprocessorConfigTag(Token):
    """
    {% ! config-option-1 cofig-option-2 %}
    """
    def process_params(self, params):
        self.preprocessor_options = [ p.output_as_string() for p in params[1:] ]

    def output(self, handler):
        # Should output nothing.
        pass

class DjangoRawOutput(Token):
    """
    {% !raw %} ... {% !endraw %}
    This section contains code which should not be validated or interpreted
    (Because is would cause trigger a false-positive "invalid HTML" or similar.)
    """
    # Note that this class does not inherit from DjangoContainer, this makes
    # sure that the html processor won't enter this class.
    def process_params(self, params):
        pass

    def output(self, handler):
        # Do not output the '{% !raw %}'-tags
        for c in self.children:
            handler(c)


class DjangoExtendsTag(Token):
    """
    {% extends varname_or_template %}
    """
    def process_params(self, params):
        param = params[1].output_as_string()

        if param[0] == '"' and param[-1] == '"':
            self.template_name = param[1:-1]
            self.template_name_is_variable = False
        elif param[0] == "'" and param[-1] == "'":
            self.template_name = param[1:-1]
            self.template_name_is_variable = False
        else:
            raise CompileException(self, 'Preprocessor does not support variable {% extends %} nodes')

            self.template_name = param
            self.template_name_is_variable = True

    def output(self, handler):
        if self.template_name_is_variable:
            handler(u'{%extends '); handler(self.template_name); handler(u'%}')
        else:
            handler(u'{%extends "'); handler(self.template_name); handler(u'"%}')


class DjangoIncludeTag(Token):
    """
    {% include varname_or_template %}
    """
    def process_params(self, params):
        param = params[1].output_as_string()

        if param[0] in ('"', "'") and param[-1] in ('"', "'"):
            self.template_name = param[1:-1]
            self.template_name_is_variable = False
        else:
            self.template_name = param
            self.template_name_is_variable = True

    def output(self, handler):
        if self.template_name_is_variable:
            handler(u'{%include '); handler(self.template_name); handler(u'%}')
        else:
            handler(u'{%include "'); handler(self.template_name); handler(u'"%}')


class DjangoDecorateTag(DjangoContainer):
    """
    {% decorate "template.html" %}
        things to place in '{{ content }}' of template.html
    {% enddecorate %}
    """
    def process_params(self, params):
        param = params[1].output_as_string()

        # Template name should not be variable
        if param[0] in ('"', "'") and param[-1] in ('"', "'"):
            self.template_name = param[1:-1]
        else:
            raise CompileException(self, 'Do not use variable template names in {% decorate %}')

    def output(self, handler):
        handler(u'{%decorate "%s" %}' % self.template_name);
        handler(self.children)
        handler(u'{%enddecorate%}')


class NoLiteraleException(Exception):
    def __init__(self):
        Exception.__init__(self, 'Not a variable')

def _variable_to_literal(variable):
    """
    if the string 'variable' represents a variable, return it
    without the surrounding quotes, otherwise raise exception.
    """
    if variable[0] in ('"', "'") and variable[-1] in ('"', "'"):
        return variable[1:-1]
    else:
        raise NoLiteraleException()


class DjangoUrlTag(DjangoTag):
    """
    {% url name param1 param2 param3=value %}
    """
    def process_params(self, params):
        self.url_params = params[1:]

    def output(self, handler):
        handler(u'{%url ')
        for c in self.url_params:
            handler(c)
            handler(u' ')
        handler(u'%}')


class DjangoTransTag(Token):
    """
    {% include varname_or_template %}
    """
    def process_params(self, params):
        self.__string_is_variable = False
        param = params[1].output_as_string()

        if param[0] in ('"', "'") and param[-1] in ('"', "'"):
            self.__string = param[1:-1]
            self.__string_is_variable = False
        else:
            self.__string = param
            self.__string_is_variable = True

    @property
    def string(self):
        return '' if self.__string_is_variable else self.__string

    @property
    def is_variable(self):
        return self.__string_is_variable

    def output(self, handler):
        if self.__string_is_variable:
            handler(u'{%trans '); handler(self.__string); handler(u'%}')
        else:
            handler(u'{%trans "'); handler(self.__string); handler(u'"%}')


class DjangoBlocktransTag(Token):
    """
    Contains:
    {% blocktrans %} children {% endblocktrans %}
    """
    def process_params(self, params):
        # Skip django-tag-element
        self.params = params[1:]

    @property
    def is_variable(self):
        # Consider this a dynamic string (which shouldn't be translated at compile time)
        # if it has variable nodes inside. Same for {% plural %} inside the blocktrans.
        return len(list(self.child_nodes_of_class([DjangoVariable, DjangoTag]))) > 0

    @property
    def string(self):
        return '' if self.is_variable else self.output_as_string(True)

    def output(self, handler):
        # Blocktrans output
        handler(u'{%blocktrans ');
        for p in self.params:
            p.output(handler)
            handler(u' ')
        handler(u'%}')
        Token.output(self, handler)
        handler(u'{%endblocktrans%}')


class DjangoLoadTag(Token):
    """
    {% load module1 module2 ... %}
    """
    def process_params(self, params):
        self.modules = [ p.output_as_string() for p in params[1:] ]

    def output(self, handler):
        handler(u'{% load ')
        handler(u' '.join(self.modules))
        handler(u'%}')


class DjangoMacroTag(DjangoContainer):
    def process_params(self, params):
        assert len(params) == 2
        name = params[1].output_as_string()
        assert name[0] in ('"', "'") and name[0] == name[-1]
        self.macro_name = name[1:-1]

    def output(self, handler):
        handler(u'{%macro "'); handler(self.macro_name); handler(u'"%}')
        Token.output(self, handler)
        handler(u'{%endmacro%}')


class DjangoIfDebugTag(DjangoContainer):
    """
    {% ifdebug %} ... {% endifdebug %}
    """
    def process_params(self, params):
        pass

    def output(self, handler):
        handler(u'{%ifdebug%}')
        Token.output(self, handler)
        handler(u'{%endifdebug%}')


class DjangoCallMacroTag(Token):
    def process_params(self, params):
        assert len(params) == 2
        name = params[1].output_as_string()
        assert name[0] in ('"', "'") and name[0] == name[-1]
        self.macro_name = name[1:-1]

    def output(self, handler):
        handler(u'{%callmacro "')
        handler(self.macro_name)
        handler(u'"%}')


class DjangoCompressTag(DjangoContainer):
    """
    {% compress %} ... {% endcompress %}
    """
    def process_params(self, params):
        pass

    def output(self, handler):
        # Don't output the template tags.
        # (these are hints to the preprocessor only.)
        Token.output(self, handler)


class DjangoBlockTag(DjangoContainer):
    """
    Contains:
    {% block %} children {% endblock %}
    Note: this class should not inherit from DjangoTag, because it's .children are different...  XXX
    """
    def process_params(self, params):
        self.block_name = params[1].output_as_string()

    def output(self, handler):
        handler(u'{%block '); handler(self.block_name); handler(u'%}')
        Token.output(self, handler)
        handler(u'{%endblock%}')


# ====================================[ Parser extensions ]=====================================


# Mapping for turning the lex tree into a Django parse tree
_PARSER_MAPPING_DICT = {
    'content': DjangoContent,
    'django-tag': DjangoTag,
    'django-variable': DjangoVariable,
    'django-comment': DjangoComment,
    'django-multiline-comment': DjangoMultilineComment,
}

def _add_parser_extensions(tree):
    """
    Turn the lex tree into a parse tree.
    Actually, nothing more than replacing the parser classes, as
    a wrapper around the lex tree.
    """
    tree.__class__ = DjangoRootNode

    def _add_parser_extensions2(node):
        if isinstance(node, Token):
            if node.name in _PARSER_MAPPING_DICT:
                node.__class__ = _PARSER_MAPPING_DICT[node.name]
                if hasattr(node, 'init_extension'):
                    node.init_extension()

            for c in node.children:
                _add_parser_extensions2(c)

    _add_parser_extensions2(tree)


# Mapping for replacing the *inline* DjangoTag nodes into more specific nodes
_DJANGO_INLINE_ELEMENTS = {
    'extends': DjangoExtendsTag,
    'trans': DjangoTransTag,
    'include': DjangoIncludeTag,
    'url': DjangoUrlTag,
    'load': DjangoLoadTag,
    'callmacro': DjangoCallMacroTag,
    '!': DjangoPreprocessorConfigTag,
}

def _process_inline_tags(tree):
    """
    Replace DjangoTag elements by more specific elements.
    """
    for c in tree.children:
        if isinstance(c, DjangoTag) and c.tagname in _DJANGO_INLINE_ELEMENTS:
            # Patch class
            c.__class__ = _DJANGO_INLINE_ELEMENTS[c.tagname]

            # In-line tags don't have childnodes, but process what we had
            # as 'children' as parameters.
            c.process_params(list(c.get_childnodes_with_name('django-tag-element')))
            #c.children = [] # TODO: for Jonathan -- we want to keep this tags API compatible with the DjangoTag object, so keep children

        elif isinstance(c, DjangoTag):
            _process_inline_tags(c)


# Mapping for replacing the *block* DjangoTag nodes into more specific nodes
__DJANGO_BLOCK_ELEMENTS = {
    'block': ('endblock', DjangoBlockTag),
    'blocktrans': ('endblocktrans', DjangoBlocktransTag),
    'macro': ('endmacro', DjangoMacroTag),
    'ifdebug': ('endifdebug', DjangoIfDebugTag),
    'decorate': ('enddecorate', DjangoDecorateTag),
    'compress': ('endcompress', DjangoCompressTag),
    '!raw': ('!endraw', DjangoRawOutput),


#    'xhr': ('else', 'endxhr', DjangoXhrTag),
#    'if': ('else', 'endif', DjangoIfTag),
#    'is_enabled': ('else', 'end_isenabled', DjangoIsEnabledTag),
}




# ====================================[ Check parser settings in template {% ! ... %} ]================

class PreProcessSettings(object):
    def __init__(self, path=''):
        # Default settings
        self.whitespace_compression = True
        self.preprocess_translations = True
        self.preprocess_urls = True
        self.preprocess_variables = True
        self.remove_block_tags = True # Should propably not be disabled
        self.merge_all_load_tags = True
        self.execute_preprocessable_tags = True
        self.preprocess_macros = True
        self.preprocess_ifdebug = True # Should probably always be True
        self.remove_some_tags = True # As we lack a better settings name

        # HTML processor settings
        self.is_html = True

        self.check_alt_and_title_attributes = False
        self.compile_css = True
        self.compile_javascript = True
        self.ensure_quotes_around_html_attributes = False # Not reliable for now...
        self.merge_internal_css = False
        self.merge_internal_javascript = False
        self.remove_empty_class_attributes = False
        self.pack_external_javascript = False
        self.pack_external_css = False
        self.parse_all_html_tags = False
        self.validate_html = False

        # Load defaults form settings.py
        for o in getattr(settings, 'TEMPLATE_PREPROCESSOR_OPTIONS', { }):
            self.change(o)

        # Disable all HTML extensions if template name does not end with .html
        # (Can still be overriden in the templates.)
        if path and not path.endswith('.html'):
            self.is_html = False

    def change(self, value, node=False):
        actions = {
            'whitespace-compression': ('whitespace_compression', True),
            'no-whitespace-compression': ('whitespace_compression', False),
            'merge-internal-javascript': ('merge_internal_javascript', True),
            'merge-internal-css': ('merge_internal_css', True),
            'html': ('is_html', True), # Enable HTML extensions
            'no-html': ('is_html', False), # Disable all HTML specific options
            'no-macro-preprocessing': ('preprocess_macros', False),
            'html-remove-empty-class-attributes': ('remove_empty_class_attributes', True),
            'html-check-alt-and-title-attributes': ('check_alt_and_title_attributes', True),
            'pack-external-javascript': ('pack_external_javascript', True),
            'pack-external-css': ('pack_external_css', True),
            'compile-css': ('compile_css', True),
            'compile-javascript': ('compile_javascript', True),
            'parse-all-html-tags': ('parse_all_html_tags', True),
            'validate-html': ('validate_html', True),
        }

        if value in actions:
            setattr(self, actions[value][0], actions[value][1])
        else:
            if node:
                raise CompileException(node, 'No such template preprocessor option: %s' % value)
            else:
                raise CompileException(None, 'No such template preprocessor option: %s (in settings.py)' % value)


def _get_preprocess_settings(tree, extra_options):
    """
    Look for parser configuration tags in the template tree.
    Return a dictionary of the compile options to use.
    """
    options = PreProcessSettings(tree.path)

    for c in tree.child_nodes_of_class([ DjangoPreprocessorConfigTag ]):
        for o in c.preprocessor_options:
            options.change(o, c)

    for o in extra_options or []:
        options.change(o)

    return options



# ====================================[ 'Patched' class definitions ]=====================================


class DjangoPreprocessedInclude(DjangoContainer):
    def init(self, children):
        self.children = children

class DjangoPreprocessedCallMacro(DjangoContainer):
    def init(self, children):
        self.children = children

class DjangoPreprocessedUrl(DjangoContent):
    def init(self, url_value):
        self.children = [ url_value]

class DjangoPreprocessedVariable(DjangoContent):
    def init(self, var_value):
        self.children = var_value


# ====================================[ Parse tree manipulations ]=====================================

def apply_method_on_parse_tree(tree, class_, method, *args, **kwargs):
    for c in tree.children:
        if isinstance(c, class_):
            getattr(c, method)(*args, **kwargs)

        if isinstance(c, Token):
            apply_method_on_parse_tree(c, class_, method, *args, **kwargs)


def _process_extends(tree, loader):
    """
    {% extends ... %}
    When this tree extends another template. Load the base template,
    compile it, merge the trees, and return a new tree.
    """
    try:
        base_tree = None

        for c in tree.children:
            if isinstance(c, DjangoExtendsTag) and not c.template_name_is_variable:
                base_tree = loader(c.template_name)
                break

        if base_tree:
            base_tree_blocks = list(base_tree.child_nodes_of_class([ DjangoBlockTag ]))
            tree_blocks = list(tree.child_nodes_of_class([ DjangoBlockTag ]))

            # For every {% block %} in the base tree
            for base_block in base_tree_blocks:
                # Look for a block with the same name in the current tree
                for block in tree_blocks:
                    if block.block_name == base_block.block_name:
                        # Replace {{ block.super }} variable by the parent's
                        # block node's children.
                        block_dot_super = base_block.children

                        for v in block.child_nodes_of_class([ DjangoVariable ]):
                            if v.varname == 'block.super':
                                # Found a {{ block.super }} declaration, deep copy
                                # parent nodes in here
                                v.__class__ = DjangoPreprocessedVariable
                                v.init(deepcopy(block_dot_super[:]))

                        # Replace all nodes in the base tree block, with this nodes
                        base_block.children = block.children

            # Move every {% load %} and {% ! ... %} to the base tree
            for l in tree.child_nodes_of_class([ DjangoLoadTag, DjangoPreprocessorConfigTag ]):
                base_tree.children.insert(0, l)

            return base_tree

        else:
            return tree

    except TemplateDoesNotExist, e:
        print e
        return tree


def _preprocess_includes(tree, loader):
    """
    Look for all the {% include ... %} tags and replace it by their include.
    """
    include_blocks = list(tree.child_nodes_of_class([ DjangoIncludeTag ]))

    for block in include_blocks:
        if not block.template_name_is_variable:
            try:
                # Parse include
                include_tree = loader(block.template_name)

                # Move tree from included file into {% include %}
                block.__class__ = DjangoPreprocessedInclude
                block.init([ include_tree ])

                block.path = include_tree.path
                block.line = include_tree.line
                block.column = include_tree.column

            except TemplateDoesNotExist, e:
                raise CompileException(block, 'Template in {%% include %%} tag not found (%s)' % block.template_name)


def _preprocess_decorate_tags(tree, loader):
    """
    Replace {% decorate "template.html" %}...{% enddecorate %} by the include,
    and fill in {{ content }}
    """
    class DjangoPreprocessedDecorate(DjangoContent):
        def init(self, children):
            self.children = children

    decorate_blocks = list(tree.child_nodes_of_class([ DjangoDecorateTag ]))

    for block in decorate_blocks:
        # Content nodes
        content = block.children

        # Replace content
        try:
            include_tree = loader(block.template_name)

            for content_var in include_tree.child_nodes_of_class([ DjangoVariable ]):
                if content_var.varname == 'decorater.content':
                    content_var.__class__ = DjangoPreprocessedVariable
                    content_var.init(content)

            # Move tree
            block.__class__ = DjangoPreprocessedDecorate
            block.init([ include_tree ])

        except TemplateDoesNotExist, e:
            raise CompileException(self, 'Template in {% decorate %} tag not found (%s)' % block.template_name)


def _group_all_loads(tree):
    """
    Look for all {% load %} tags, and group them to one, on top.
    """
    all_modules = set()
    first_load_tag = None

    # Collect all {% load %} nodes.
    for load_tag in tree.child_nodes_of_class([ DjangoLoadTag ]):
        if not first_load_tag:
            first_load_tag = load_tag

        for l in load_tag.modules:
            all_modules.add(l)

    # Remove all {% load %} nodes
    tree.remove_child_nodes_of_class(DjangoLoadTag)

    # Place all {% load %} in the first node of the tree
    if first_load_tag:
        first_load_tag.modules = list(all_modules)
        tree.children.insert(0, first_load_tag)

        # But {% extends %} really needs to be placed before everything else
        # NOTE: (Actually not necessary, because we don't support variable extends.)
        extends_tags = list(tree.child_nodes_of_class([ DjangoExtendsTag ]))
        tree.remove_child_nodes_of_class(DjangoExtendsTag)

        for e in extends_tags:
            tree.children.insert(0, e)

def _preprocess_urls(tree):
    """
    Replace URLs without variables by their resolved value.
    """
    def parse_url_params(urltag):
        if not urltag.url_params:
            raise CompileException(urltag, 'Attribute missing for {% url %} tag.')

        # Parse url parameters
        name = urltag.url_params[0].output_as_string()
        args = []
        kwargs = { }
        for k in urltag.url_params[1:]:
            k = k.output_as_string()
            if '=' in k:
                k,v = k.split('=', 1)
                kwargs[str(k)] = _variable_to_literal(v)
            else:
                args.append(_variable_to_literal(k))

        return name, args, kwargs

    for urltag in tree.child_nodes_of_class([ DjangoUrlTag ]):
        try:
            name, args, kwargs = parse_url_params(urltag)
            if not 'as' in args:
                result = reverse(name, args=args, kwargs=kwargs)
                urltag.__class__ = DjangoPreprocessedUrl
                urltag.init(result)
        except NoReverseMatch, e:
            pass
        except NoLiteraleException, e:
            # Got some variable, can't prerender url
            pass


def _preprocess_variables(tree, values_dict):
    """
    Replace known variables, like {{ MEDIA_URL }} by their value.
    """
    for var in tree.child_nodes_of_class([ DjangoVariable ]):
        if var.varname in values_dict:
            value = values_dict[var.varname]
            var.__class__ = DjangoPreprocessedVariable
            var.init([value])

                # TODO: escape
                #       -> for now we don't escape because
                #          we are unsure of the autoescaping state.
                #          and 'resolve' is only be used for variables
                #          like MEDIA_URL which are safe in HTML.


def _preprocess_trans_tags(tree):
    """
    Replace {% trans %} and {% blocktrans %} if they don't depend on variables.
    """
    class DjangoTranslated(DjangoContent):
        def init(self, translated_text):
            self.children = [ translated_text ]

    for trans in tree.child_nodes_of_class([ DjangoTransTag, DjangoBlocktransTag ]):
        if not trans.is_variable:
            s = _(trans.string)
            trans.__class__ = DjangoTranslated
            trans.init(s)


def _preprocess_ifdebug(tree):
    if settings.DEBUG:
        for ifdebug in tree.child_nodes_of_class([ DjangoIfDebugTag ]):
            tree.replace_child_by_nodes(ifdebug, ifdebug.children)
    else:
        tree.remove_child_nodes_of_class(DjangoIfDebugTag)


def _preprocess_macros(tree):
    """
    Replace every {% callmacro "name" %} by the content of {% macro "name" %} ... {% endmacro %}
    NOTE: this will not work with recursive macro calls.
    """
    macros = { }
    for m in tree.child_nodes_of_class([ DjangoMacroTag ]):
        macros[m.macro_name] = m

    for call in tree.child_nodes_of_class([ DjangoCallMacroTag ]):
        if call.macro_name in macros:
            # Replace the call node by a deep-copy of the macro childnodes
            call.__class__ = DjangoPreprocessedCallMacro
            call.init(deepcopy(macros[call.macro_name].children[:]))

    # Remove all macro nodes
    tree.remove_child_nodes_of_class(DjangoMacroTag)


def _execute_preprocessable_tags(tree):
    for c in tree.children:
        if isinstance(c, DjangoTag) and c.tagname in PREPROCESS_TAGS:
            params = [ p.output_as_string() for p in c.get_childnodes_with_name('django-tag-element') ]
            try:
                c.children = [ PREPROCESS_TAGS[c.tagname](*params) ]
                c.__class__ = DjangoContent
            except NotPreprocessable:
                pass

        elif isinstance(c, DjangoContainer):
            _execute_preprocessable_tags(c)

from template_preprocessor.core.html_processor import compile_html



def parse(source_code, path, loader, main_template=False, options=None):
    """
    Parse the code.
    - source_code: string
    - loader: method to be called to include other templates.
    - path: for attaching meta information to the tree.
    - main_template: False for includes/extended templates. True for the
                     original path that was called.
    """
    # To start, create the root node of a tree.
    tree = Token(name='root', line=1, column=1, path=path)
    tree.children = [ source_code ]

    # Lex Django tags
    tokenize(tree, __DJANGO_STATES, [Token])

    # Phase I: add parser extensions
    _add_parser_extensions(tree)

    # Phase II: process inline tags
    _process_inline_tags(tree)

    # Phase III: create recursive structure for block level tags.
    nest_block_level_elements(tree, __DJANGO_BLOCK_ELEMENTS, [DjangoTag], lambda c: c.tagname)

    # === Actions ===

    # Extend parent template and process includes
    tree = _process_extends(tree, loader) # NOTE: this returns a new tree!
    _preprocess_includes(tree, loader)
    _preprocess_decorate_tags(tree, loader)


    # Following actions only need to be applied if this is the 'main' tree.
    # It does not make sense to apply it on every include, and then again
    # on the complete tree.
    if main_template:
        options = _get_preprocess_settings(tree, options)

        # Do translations
        if options.preprocess_translations:
            _preprocess_trans_tags(tree)

        # Reverse URLS
        if options.preprocess_urls:
            _preprocess_urls(tree)

        # Do variable lookups
        if options.preprocess_variables:
            from django.contrib.sites.models import Site
            _preprocess_variables(tree,
                        {
                            'MEDIA_URL': settings.MEDIA_URL,
                            'SITE_DOMAIN': Site.objects.get_current().domain,
                            'SITE_NAME': Site.objects.get_current().name,
                            'SITE_URL': 'http://%s' % Site.objects.get_current().domain,
                        })

        # Don't output {% block %} tags in the compiled file.
        if options.remove_block_tags:
            tree.collapse_nodes_of_class(DjangoBlockTag)

        # Preprocess {% callmacro %} tags
        if options.preprocess_macros:
            _preprocess_macros(tree)

        if options.preprocess_ifdebug:
            _preprocess_ifdebug(tree)

        # Group all {% load %} statements
        if options.merge_all_load_tags:
            _group_all_loads(tree)

        # Preprocessable tags
        if options.execute_preprocessable_tags:
            _execute_preprocessable_tags(tree)

        # HTML compiler
        if options.is_html:
            compile_html(tree, options)

    return tree