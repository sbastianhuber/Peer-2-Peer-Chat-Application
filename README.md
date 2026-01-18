# Peer-2-Peer-Chat-Application
This project implements a fully decentralized peer-to-peer chat system based on a logical ring topology. The system operates without a central server and supports dynamic peer discovery, crash fault detection, and automatic ring repair. Peers discover each other via periodic UDP broadcasts, communicate with immediate neighbors using TCP, and elect a coordinator using the Le Lann–Chang–Roberts algorithm to manage leadership after topology changes or failures.
Background

This project was developed within the Master of Science (M.Sc.) program as part of the course Distributed Systems at the University of Stuttgart, under the supervision of Prof. Dr. Marco Aiello.

---
**Contributors:** @Madinaxn, @dery28
