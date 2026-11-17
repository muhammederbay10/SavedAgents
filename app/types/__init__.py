"""Content types: the kinds of saved post that get extra behaviour (a place, a movie…).

Every post gets an open category and tags. A built-in type adds fields, a
resolver or filters on top. Each type is one module that declares a
`ContentType`. `app.types.registry` lists them, and the rest of the code looks
types up there instead of naming them.
"""
