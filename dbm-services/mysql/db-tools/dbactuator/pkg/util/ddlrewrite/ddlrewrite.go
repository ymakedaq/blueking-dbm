/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

// Package ddlrewrite provides utilities for rewriting DDL statements.
package ddlrewrite

import (
	"fmt"
	"os"
	"regexp"
	"strings"

	"vitess.io/vitess/go/vt/sqlparser"
)

// createTablePrefixPattern matches the leading CREATE [TEMPORARY] TABLE clause.
var createTablePrefixPattern = regexp.MustCompile(`(?is)^(\s*CREATE\s+(?:TEMPORARY\s+)?TABLE)(\s+)`)

// ifNotExistsPrefixPattern matches IF NOT EXISTS immediately after CREATE TABLE.
var ifNotExistsPrefixPattern = regexp.MustCompile(`(?is)^\s*IF\s+NOT\s+EXISTS\b`)

// AddCreateTableIfNotExists parses a single CREATE TABLE statement and ensures IF NOT EXISTS is present.
func AddCreateTableIfNotExists(statement string) (string, error) {
	statement = strings.TrimSpace(statement)
	if statement == "" {
		return "", fmt.Errorf("empty statement")
	}

	p, err := newParser()
	if err != nil {
		return "", err
	}

	stmt, err := p.Parse(statement)
	if err != nil {
		return "", fmt.Errorf("failed to parse statement: %w", err)
	}

	createStmt, ok := stmt.(*sqlparser.CreateTable)
	if !ok {
		return "", fmt.Errorf("statement is not a CREATE TABLE")
	}

	if createStmt.IfNotExists {
		return statement, nil
	}

	return insertCreateTableIfNotExists(statement)
}

// IsCreateTable reports whether the statement is a CREATE TABLE statement.
func IsCreateTable(statement string) bool {
	statement = strings.TrimSpace(statement)
	if statement == "" {
		return false
	}

	p, err := newParser()
	if err != nil {
		return false
	}

	stmt, err := p.Parse(statement)
	if err != nil {
		return false
	}

	_, ok := stmt.(*sqlparser.CreateTable)
	return ok
}

// RewriteCreateTableIfNotExistsInFile reads SQL from inputPath, rewrites CREATE TABLE statements
// to include IF NOT EXISTS, and writes the result to outputPath. Other statements are unchanged.
func RewriteCreateTableIfNotExistsInFile(inputPath, outputPath string) error {
	input, err := os.ReadFile(inputPath)
	if err != nil {
		return fmt.Errorf("failed to read input file: %w", err)
	}

	output, err := RewriteCreateTableIfNotExistsSQL(string(input))
	if err != nil {
		return err
	}

	if err := os.WriteFile(outputPath, []byte(output), 0644); err != nil {
		return fmt.Errorf("failed to write output file: %w", err)
	}
	return nil
}

// RewriteCreateTableIfNotExistsSQL rewrites CREATE TABLE statements in a SQL file content.
// Non-CREATE TABLE statements are passed through unchanged.
func RewriteCreateTableIfNotExistsSQL(content string) (string, error) {
	if strings.TrimSpace(content) == "" {
		return "", fmt.Errorf("empty sql file")
	}

	p, err := newParser()
	if err != nil {
		return "", err
	}

	pieces, err := p.SplitStatementToPieces(content)
	if err != nil {
		return "", fmt.Errorf("failed to split sql file: %w", err)
	}

	rewritten := make([]string, 0, len(pieces))
	for _, piece := range pieces {
		piece = strings.TrimSpace(piece)
		if piece == "" {
			continue
		}

		stmt, err := p.Parse(piece)
		if err != nil {
			return "", fmt.Errorf("failed to parse statement: %w", err)
		}

		if createStmt, ok := stmt.(*sqlparser.CreateTable); ok {
			if createStmt.IfNotExists {
				rewritten = append(rewritten, piece)
				continue
			}

			rewrittenPiece, err := insertCreateTableIfNotExists(piece)
			if err != nil {
				return "", err
			}
			rewritten = append(rewritten, rewrittenPiece)
			continue
		}

		rewritten = append(rewritten, piece)
	}

	if len(rewritten) == 0 {
		return "", fmt.Errorf("empty sql file")
	}

	return strings.Join(rewritten, ";\n") + ";\n", nil
}

func newParser() (*sqlparser.Parser, error) {
	p, err := sqlparser.New(sqlparser.Options{
		TruncateUILen:  512,
		TruncateErrLen: 0,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create parser: %w", err)
	}
	return p, nil
}

func insertCreateTableIfNotExists(statement string) (string, error) {
	insertPos, hasIfNotExists, err := findCreateTableInsertPos(statement)
	if err != nil {
		return "", err
	}
	if hasIfNotExists {
		return statement, nil
	}
	return statement[:insertPos] + " IF NOT EXISTS" + statement[insertPos:], nil
}

func findCreateTableInsertPos(statement string) (insertPos int, hasIfNotExists bool, err error) {
	offset := 0
	remaining := statement
	for {
		remaining = strings.TrimLeft(remaining, " \t\r\n")
		if remaining == "" {
			return 0, false, fmt.Errorf("failed to insert IF NOT EXISTS into CREATE TABLE statement")
		}

		consumed := len(statement[offset:]) - len(remaining)
		offset += consumed

		switch {
		case strings.HasPrefix(remaining, "--"):
			nl := strings.IndexByte(remaining, '\n')
			if nl < 0 {
				return 0, false, fmt.Errorf("failed to insert IF NOT EXISTS into CREATE TABLE statement")
			}
			offset += nl + 1
			remaining = remaining[nl+1:]
			continue
		case strings.HasPrefix(remaining, "/*"):
			end := strings.Index(remaining, "*/")
			if end < 0 {
				return 0, false, fmt.Errorf("failed to insert IF NOT EXISTS into CREATE TABLE statement")
			}
			offset += end + 2
			remaining = remaining[end+2:]
			continue
		}

		loc := createTablePrefixPattern.FindStringSubmatchIndex(remaining)
		if loc == nil {
			return 0, false, fmt.Errorf("failed to insert IF NOT EXISTS into CREATE TABLE statement")
		}

		afterTable := remaining[loc[3]:]
		if ifNotExistsPrefixPattern.MatchString(afterTable) {
			return 0, true, nil
		}

		return offset + loc[3], false, nil
	}
}
