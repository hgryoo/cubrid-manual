*****
Index
*****

CREATE INDEX
============

Creates an index to a specified table by using the **CREATE INDEX** statement. Regarding writing index name, see :doc:`/sql/identifier`.

For how to use indexes on the **SELECT** statement like Using SQL Hint, Descending Index, Covering Index, Index Skip Scan, **ORDER BY** Optimization and **GROUP BY** Optimization, and how to create Filtered Index and Function-based Index, see :doc:`/sql/tuning`.

::

    CREATE [UNIQUE] INDEX index_name ON table_name <index_col_desc> ;
     
        <index_col_desc> ::=
            ( column_name [ASC | DESC] [ {, column_name [ASC | DESC]} ...] ) [ WHERE <filter_predicate> ]
            | (function_name (argument_list) )

*   **UNIQUE**: Creates an index with unique values.
*   *index_name*: Specifies the name of the index to be created. The index name must be unique in the table.

*   *table_name*: Specifies the name of the table where the index is to be created.
*   *column_name*: Specifies the name of the column where the index is to be applied. To create a composite index, specify two or more column names.
*   **ASC** | **DESC**: Specifies the sorting order of columns. 

*   <*filter_predicate*>: Defines the conditions to create filtered indexes. When there are several comparison conditions between a column and a constant, filtering is available only when the conditions are connected by using **AND**. Regarding this, definitely watch :ref:`filtered-index`.
*   *function_name* (*argument_list*): Defines the conditions to create function-based indexes. Regarding this, definitely watch :ref:`function-index`.

..  note::

    *   From CUBRID 9.0, the index name should not be omitted.

    *   Prefix index feature is deprecated, so it is not recommended anymore.

The following example shows how to create a descending index.

.. code-block:: sql

    CREATE INDEX gold_index ON participant(gold DESC);

The following example shows how to create a multiple column index.

.. code-block:: sql

    CREATE INDEX name_nation_idx ON athlete(name, nation_code);

.. _alter-index:

ALTER INDEX
===========

The **ALTER INDEX** statement rebuilds an index. In other words, it drops and rebuilds an index. If a table name and column names are added at the end of the **ON** clause, a new index is recreated with the table and column names. 

::

    ALTER [UNIQUE] INDEX index_name
    [ON table_name [<index_col_desc>]] REBUILD
     
    <index_col_desc> ::=
        ( column_name [ASC | DESC] [ {, column_name [ASC | DESC]} ...] ) [ WHERE <filter_predicate> ]
        | (function_name (argument_list) )

*   **UNIQUE**: This should be specified if the index to be recreated is an unique index.
*   *index_name*: Specifies the name of the index to be recreated. The index name must be unique in the table.
*   *table_name*: Specifies the name of the table where the index is recreated.
*   *column_name*: Specifies the name of the column where the index is to be applied. To create a composite index, specify two or more column names.
*   **ASC** | **DESC**: Specifies the sorting order of columns. 

*   <*filter_predicate*>: Defines the conditions to create filtered indexes. When there are several comparison conditions between a column and a constant, filtering is available only when the conditions are connected by using **AND**.
*   *function_name* (*argument_list*): Defines the conditions to create function-based indexes.

.. note::

    *   From CUBRID 9.0, the index name should not be omitted.

    *   Prefix index feature is deprecated, so it is not recommended anymore.

The following is an example of recreating indexes in various ways:

.. code-block:: sql

    CREATE INDEX i_game_medal ON game(medal);
    ALTER INDEX i_game_medal ON game REBUILD;
    ALTER INDEX char_idx ON athlete(gender, nation_code) WHERE gender='M' AND nation_code='USA' REBUILD;

DROP INDEX
==========

.. CUBRIDSUS-9352: From 10.0, "UNIQUE" keyword is useless.

Use the **DROP INDEX** statement to drop an index. 

::

    DROP [UNIQUE] INDEX index_name [ON table_name] ;

*   **UNIQUE**: Specifies that the index to be dropped is a unique index. This also can be dropped with **DROP CONSTRAINT** clause.
*   *index_name*: Specifies the name of the index to be dropped. If omitted, a name is automatically assigned as *i_<table_name>_<column_names>*.
*   *table_name*: Specifies the name of the table whose index is dropped.

The following is an example of dropping an index:

.. code-block:: sql

    DROP INDEX game_date_idx ON game;
