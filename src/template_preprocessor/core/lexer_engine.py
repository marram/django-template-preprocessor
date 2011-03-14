#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Django template preprocessor.
Author: Jonathan Slenders, City Live
"""


"""
Tokenizer for a template preprocessor.
------------------------------------------------------------------

This tokenizer is designed to parse a language inside a parse tree.
- It is used to parse django templates. (starting with a parse tree with only a single
  root node containg the full template code as a single string.)
- The parser is called from the html_processor, to turn the django tree into a
  html tree by parsing HTML nodes.
- The parser is called from the css_processor and the js_processor, to parse
  the css and js nodes in the HTML tree.

So, the result of this tokenizer is a tree, but it can contain tokens of
different languages.


        By the way: DON'T CHANGE ANYTHING IN THIS FILE, unless you're absolutely sure.
"""

from template_preprocessor.core.lexer import State, StartToken, Push, Record, Shift, StopToken, Pop, CompileException, Token, Error


def tokenize(tree, states, classes_to_replace_by_parsed_content, classes_to_enter=None, _state_stack=None, _token_stack=None, _root=True):
    """
    Tokenize javascript or css code within the
    django parse tree.
    """
    classes_to_enter = classes_to_enter or []

    # State stack
    state_stack = _state_stack or [ 'root' ]

    # Parse stack
    token_stack = _token_stack or [ tree ]

    # Input nodes
    input_nodes = tree.children
    tree.children = [] # Output

    # Position
    line =  tree.line
    column = tree.column
    path = tree.path

    # As long as we have input nodes
    while len(input_nodes):
        # Pop input node
        current_input_node = input_nodes[0]
        input_nodes = input_nodes[1:]

        if isinstance(current_input_node, basestring):
            # Tokenize DjangoContent
            string = current_input_node #.get_string_value()

            # We want the regex to be able to match as much as possible,
            # So, if several basestring nodes, are following each other,
            # concatenate as one.
            while (len(input_nodes) and isinstance(input_nodes[0], basestring)):
                # Pop another input node
                string = string + input_nodes[0]
                input_nodes = input_nodes[1:]

            # Parse position
            position = 0

            while position < len(string):
                for compiled_regex, action_list in states[ state_stack[-1] ].transitions():
                    match = compiled_regex.match(string[position:])

                    #print state_stack, string[position:position+10]

                    if match:
                        (start, count) = match.span()

                        # Read content
                        content = string[position : position + count]

                        # Execute actions for this match
                        for action in action_list:
                            if isinstance(action, Record):
                                if action.value:
                                    token_stack[-1].append(action.value)
                                else:
                                    token_stack[-1].append(content)

                            elif isinstance(action, Shift):
                                position += count
                                count = 0

                                # Update row/column
                                f = content.find('\n')
                                while f >= 0:
                                    line += 1
                                    column = 1
                                    content = content[f+1:]
                                    f = content.find('\n')
                                column += len(content)

                            elif isinstance(action, Push):
                                state_stack.append(action.state_name)

                            elif isinstance(action, Pop):
                                state_stack.pop()

                            elif isinstance(action, StartToken):
                                token = Token(action.state_name, line, column, path)
                                token_stack[-1].append(token)
                                token_stack.append(token)

                            elif isinstance(action, StopToken):
                                if action.state_name and token_stack[-1].name != action.state_name:
                                    raise CompileException(line, column, path, 'Token mismatch')

                                token_stack.pop()

                            elif isinstance(action, Error):
                                raise CompileException(line, column, path, action.message +
                                            "; near: '%s'" % string[position-20:position+20])

                        break # Out of for

        # Not a DjangoContent node? Copy in current position.
        else:
            # Recursively tokenize in this node (continue with states, token will be replaced by parsed content)
            if any([isinstance(current_input_node, cls) for cls in classes_to_replace_by_parsed_content]):
                tokenize(current_input_node, states, classes_to_replace_by_parsed_content, classes_to_enter, state_stack, token_stack, False)

            # Recursively tokenize in this node (start parsing again in nested node)
            elif any([isinstance(current_input_node, cls) for cls in classes_to_enter]):
                tokenize(current_input_node, states, classes_to_replace_by_parsed_content, classes_to_enter, state_stack, None, False)
                token_stack[-1].append(current_input_node)

            # Any other class, copy in current token
            else:
                token_stack[-1].append(current_input_node)

    if _root and token_stack != [ tree ]:
        top = token_stack[-1]
        raise CompileException(top.line, top.column, top.path, '%s not terminated' % top.name)



def nest_block_level_elements(tree, mappings, _classes=[Token], check=None):
    """
    Replace consecutive nodes like  (BeginBlock, Content, Endblock) by
    a recursive structure:  (Block with nested Content).

    Or also supported:  (BeginBlock, Content, ElseBlock Content EndBlock)
        After execution, the first content will be found in node.children,
        The second in node.children2
    """
    check = check or (lambda c: c.name)

    # Push/Pop stacks
    moving_to_node = []
    list_index = 0
    tags_stack = [] # Stack of lists (top of the list contains a list of
                # check_values for possible {% else... %} or {% end... %}-nodes.

    def get_moving_to_list():
        """
        Normally, we are moving childnodes to the .children
        list, but when we have several child_node_lists because
        of the existance of 'else'-nodes, we may move to another
        list. This method returns the list instace we are currently
        moving to.
        """
        node = moving_to_node[-1]
        index = str(list_index) if list_index else ''

        if not hasattr(node, 'children%s' % index):
            setattr(node, 'children%s' % index, [])

        return getattr(node, 'children%s' % index)

    for c in tree.children[:]:
        # The 'tags' are only concidered tags if they are of one of these classes
        is_given_class = any([isinstance(c, cls) for cls in _classes])

        # And if it's a tag, this check_value is the once which could
        # match a value of the mapping.
        check_value = check(c) if is_given_class else None

        # Found the start of a block-level tag
        if is_given_class and check_value in mappings:
            m = mappings[check(c)]
            (end, class_) = (m[:-1], m[-1])

            child_list_index = 0

            # Patch class
            c.__class__ = class_

            # Are we moving nodes
            if moving_to_node:
                get_moving_to_list().append(c)
                tree.children.remove(c)

            # Start moving all following nodes as a child node of this one
            moving_to_node.append(c)
            tags_stack.append(end)

            # This node will create a side-tree containing the 'parameters'.
            c.process_params(c.children[:])
            c.children = []

        # End of this block-level tag
        elif moving_to_node and is_given_class and check_value == tags_stack[-1][-1]:
            tree.children.remove(c)

            # Block-level tag created, apply recursively
            # No, we shouldn't!!! Child nodes of this tag are already processed
            #nest_block_level_elements(moving_to_node[-1])

            # Continue
            moving_to_node.pop()
            tags_stack.pop()

        # Any 'else'-node within
        elif moving_to_node and is_given_class and check_value in tags_stack[-1][:-1]:
            tree.children.remove(c)

            # Move the tags list
            count = end_tag[-1].index(check_value) + 1
            tags_stack[-1] = tags_stack[-1][count:]

            # Children attribute ++
            list_index += count

        # Are we moving nodes
        elif moving_to_node:
            get_moving_to_list().append(c)
            tree.children.remove(c)

            # Apply recursively
            nest_block_level_elements(c, mappings, _classes, check)

        elif isinstance(c, Token):
            # Apply recursively
            nest_block_level_elements(c, mappings, _classes, check)

    if moving_to_node:
        raise CompileException(moving_to_node[-1].line, moving_to_node[-1].column, moving_to_node[-1].path, '%s tag not terminated' % moving_to_node[-1].__class__.__name__)
