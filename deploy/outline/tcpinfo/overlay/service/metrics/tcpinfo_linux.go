// Copyright 2026 Keel contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//go:build linux

package metrics

import (
	"errors"
	"net"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

type linuxTCPInfoSampler struct {
	conn syscall.Conn
}

func newTCPInfoSampler(conn net.Conn) tcpInfoSampler {
	if conn == nil {
		return nil
	}
	syscallConn, ok := conn.(syscall.Conn)
	if !ok {
		return nil
	}
	return &linuxTCPInfoSampler{conn: syscallConn}
}

func (s *linuxTCPInfoSampler) Sample() (tcpInfoSample, error) {
	rawConn, err := s.conn.SyscallConn()
	if err != nil {
		return tcpInfoSample{}, err
	}
	var info *unix.TCPInfo
	var socketErr error
	if err := rawConn.Control(func(fd uintptr) {
		info, socketErr = unix.GetsockoptTCPInfo(int(fd), unix.IPPROTO_TCP, unix.TCP_INFO)
	}); err != nil {
		return tcpInfoSample{}, err
	}
	if socketErr != nil {
		return tcpInfoSample{}, socketErr
	}
	if info == nil {
		return tcpInfoSample{}, errors.New("TCP_INFO returned no data")
	}
	return tcpInfoSample{
		rtt:           time.Duration(info.Rtt) * time.Microsecond,
		bytesAcked:    info.Bytes_acked,
		bytesReceived: info.Bytes_received,
		bytesRetrans:  info.Bytes_retrans,
		notsent:       uint64(info.Notsent_bytes),
	}, nil
}
